#!/usr/bin/env python3
"""
Dice command for the MeshCore Bot
Handles dice rolling for D&D and other tabletop games
"""

import random
from .base_command import BaseCommand
from ..models import MeshMessage


class DiceCommand(BaseCommand):
    """Handles dice rolling commands"""
    
    # Plugin metadata
    name = "dice"
    keywords = ['dice']
    description = "Roll dice for D&D and tabletop games. Use 'dice' for d6, 'dice d20' for d20, 'dice 2d6' for 2d6, 'dice d10 d6' for mixed dice, 'dice decade' for decade die (00-90), etc."
    category = "games"
    
    # Documentation
    short_description = "Roll dice for tabletop games"
    usage = "dice [NdX|dX|decade]"
    examples = [
        "dice",
        "dice d20",
        "dice 2d6",
        "dice d10 d6",
        "dice decade"
    ]
    parameters = [
        {"name": "dice", "description": "Dice notation: d6, 2d8, d10 d6, decade"}
    ]
    
    # Standard D&D dice types
    DICE_TYPES = {
        'd4': 4,
        'd6': 6,
        'd8': 8,
        'd10': 10,
        'd12': 12,
        'd16': 16,
        'd20': 20
    }
    
    def __init__(self, bot):
        """Initialize the dice command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.dice_enabled = self.get_config_value('Dice_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.dice_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        """Get help text for the dice command.
        
        Returns:
            str: Help text string.
        """
        return self.translate('commands.dice.help')
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Override to handle dice-specific matching.
        
        Args:
            message: The received message.
            
        Returns:
            bool: True if message is a dice command, False otherwise.
        """
        content = message.content.strip().lower()
        
        # Handle command-style messages
        if content.startswith('!'):
            content = content[1:].strip().lower()
        
        # Check for exact "dice" match
        if content == "dice":
            return True
        
        # Check for dice with parameters (dice d20, dice 20, dice d6, etc.)
        # Match any message starting with "dice " - validation happens in execute()
        if content.startswith("dice "):
            words = content.split()
            if len(words) >= 2 and words[0] == "dice":
                return True  # Match any dice command, validation in execute()
        
        return False
    
    def parse_dice_notation(self, dice_input: str) -> tuple:
        """Parse dice notation and return (sides, count, is_decade).
        
        Supports: d20, 20, d6, 6, 2d6, 4d10, decade, etc.
        
        Args:
            dice_input: The dice string to parse.
            
        Returns:
            tuple: (sides, count, is_decade) or (None, None, False) if invalid.
        """
        dice_input = dice_input.strip().lower()
        
        # Handle special D&D dice types
        if dice_input == "decade":
            return (10, 1, True)  # Decade die (00-90)
        
        # Handle multiple dice notation (e.g., "2d6", "4d10", "3d20")
        if 'd' in dice_input:
            parts = dice_input.split('d')
            if len(parts) == 2:
                count_str, sides_str = parts
                
                # Handle cases like "d6" (no count specified)
                if not count_str:
                    count = 1
                else:
                    try:
                        count = int(count_str)
                        if count < 1 or count > 10:  # Reasonable limit
                            return None, None, False
                    except ValueError:
                        return None, None, False
                
                # Parse sides
                try:
                    sides = int(sides_str)
                    if sides in self.DICE_TYPES.values():
                        return sides, count, False
                    else:
                        return None, None, False
                except ValueError:
                    return None, None, False
        
        # Handle direct number (e.g., "20" -> d20)
        if dice_input.isdigit():
            sides = int(dice_input)
            if sides in self.DICE_TYPES.values():
                return sides, 1, False
            else:
                return None, None, False
        
        # Handle dice type names (e.g., "d20", "d6")
        if dice_input in self.DICE_TYPES:
            return self.DICE_TYPES[dice_input], 1, False
        
        return None, None, False
    
    def parse_mixed_dice(self, dice_input: str) -> list:
        """Parse mixed dice notation and return list of (sides, count, is_decade) tuples.
        
        Supports: "d10 d6", "2d6 d20", "d4 d8 d12", "decade", etc.
        
        Args:
            dice_input: The space-separated dice string.
            
        Returns:
            list: List of (sides, count, is_decade) tuples, or empty list if invalid.
        """
        dice_input = dice_input.strip()
        if not dice_input:
            return []
        
        # Split by spaces to get individual dice specifications
        dice_specs = dice_input.split()
        parsed_dice = []
        
        for spec in dice_specs:
            sides, count, is_decade = self.parse_dice_notation(spec)
            if sides is None:
                return []  # Invalid specification found
            parsed_dice.append((sides, count, is_decade))
        
        return parsed_dice
    
    def roll_dice(self, sides: int, count: int = 1, is_decade: bool = False) -> list:
        """Roll dice and return list of results.
        
        For decade dice, returns values 0, 10, 20, ..., 90 (formatted as 00, 10, 20, etc.)
        
        Args:
            sides: Number of sides on the die.
            count: Number of dice to roll.
            is_decade: Whether it's a decade die (00-90).
            
        Returns:
            list: List of integer results.
        """
        if is_decade:
            # Decade die: 00, 10, 20, 30, 40, 50, 60, 70, 80, 90
            return [random.randint(0, 9) * 10 for _ in range(count)]
        else:
            return [random.randint(1, sides) for _ in range(count)]
    
    def format_dice_result(self, sides: int, count: int, results: list, is_decade: bool = False) -> str:
        """Format dice roll results into a readable string.
        
        Args:
            sides: Number of sides.
            count: Number of dice.
            results: List of roll results.
            is_decade: Whether it's a decade die.
            
        Returns:
            str: Formatted result string.
        """
        if is_decade:
            # Format decade dice results (00, 10, 20, etc.)
            formatted_results = [f"{r:02d}" for r in results]
            if count == 1:
                return f"🎲 decade: {formatted_results[0]}"
            else:
                results_str = ", ".join(formatted_results)
                total = sum(results)
                return f"🎲 {count}decade: [{results_str}] = {total}"
        else:
            if count == 1:
                # Single die roll
                return self.translate('commands.dice.single_die', sides=sides, result=results[0])
            else:
                # Multiple dice
                total = sum(results)
                results_str = ", ".join(map(str, results))
                return self.translate('commands.dice.multiple_dice', count=count, sides=sides, results=results_str, total=total)
    
    def format_mixed_dice_result(self, dice_results: list) -> str:
        """Format mixed dice roll results into a readable string.
        
        Args:
            dice_results: list of tuples (sides, count, results_list, is_decade).
            
        Returns:
            str: Formatted result string for all dice.
        """
        parts = []
        grand_total = 0
        
        for sides, count, results, is_decade in dice_results:
            total = sum(results)
            grand_total += total
            
            if is_decade:
                # Format decade dice
                formatted_results = [f"{r:02d}" for r in results]
                if count == 1:
                    parts.append(f"decade: {formatted_results[0]}")
                else:
                    results_str = ", ".join(formatted_results)
                    parts.append(f"{count}decade: [{results_str}] = {total}")
            else:
                if count == 1:
                    parts.append(f"d{sides}: {results[0]}")
                else:
                    results_str = ", ".join(map(str, results))
                    parts.append(f"{count}d{sides}: [{results_str}] = {total}")
        
        result_str = " + ".join(parts)
        if len(dice_results) > 1:
            result_str += f" | Total: {grand_total}"
        
        return f"🎲 {result_str}"
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the dice command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()
        
        # Handle command-style messages
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Default to d6 if no specification
        if content.lower() == "dice":
            sides = 6
            count = 1
            results = self.roll_dice(sides, count)
            response = self.format_dice_result(sides, count, results)
            return await self.send_response(message, response)
        
        # Parse dice specification
        dice_part = content[5:].strip()  # Get everything after "dice "
        
        # Try parsing as mixed dice first (multiple dice types)
        mixed_dice = self.parse_mixed_dice(dice_part)
        
        if len(mixed_dice) > 1:
            # Multiple dice types - roll each type
            dice_results = []
            for sides, count, is_decade in mixed_dice:
                results = self.roll_dice(sides, count, is_decade)
                dice_results.append((sides, count, results, is_decade))
            
            # Format and send response
            response = self.format_mixed_dice_result(dice_results)
            return await self.send_response(message, response)
        elif len(mixed_dice) == 1:
            # Single dice type (could be multiple dice of same type like "2d6")
            sides, count, is_decade = mixed_dice[0]
            results = self.roll_dice(sides, count, is_decade)
            response = self.format_dice_result(sides, count, results, is_decade)
            return await self.send_response(message, response)
        else:
            # Invalid dice specification - return error with usage info
            available_dice = ", ".join(list(self.DICE_TYPES.keys()) + ["decade"])
            help_text = self.get_help_text()
            error_msg = self.translate('commands.dice.invalid_dice_type', available=available_dice)
            response = f"{error_msg}\n\n{help_text}"
            return await self.send_response(message, response)
