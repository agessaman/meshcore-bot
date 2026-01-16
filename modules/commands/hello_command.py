#!/usr/bin/env python3
"""
Hello command for the MeshCore Bot
Responds to various greetings with robot-themed responses
"""

import random
from typing import Any, List, Dict
from .base_command import BaseCommand
from ..models import MeshMessage


class HelloCommand(BaseCommand):
    """Handles various greeting commands"""
    
    # Plugin metadata
    name = "hello"
    keywords = ['hello', 'hi', 'hey', 'howdy', 'greetings', 'salutations', 'good morning', 'good afternoon', 'good evening', 'good night', 'yo', 'sup', 'whats up', 'what\'s up', 'morning', 'afternoon', 'evening', 'night', 'gday', 'g\'day', 'hola', 'bonjour', 'ciao', 'namaste', 'aloha', 'shalom', 'konnichiwa', 'guten tag', 'buenos dias', 'buenas tardes', 'buenas noches']
    description = "Responds to greetings with robot-themed responses"
    category = "basic"
    
    # Documentation
    short_description = "Responds to greetings with robot-themed responses"
    usage = "hello"
    examples = ["hello", "hi", "hey"]
    
    def __init__(self, bot: Any):
        """Initialize the hello command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        
        # Load configuration
        self.hello_enabled = self.get_config_value('Hello_Command', 'enabled', fallback=True, value_type='bool')
        
        # Fallback arrays if translations not available
        self._init_fallback_arrays()
    
    def _init_fallback_arrays(self) -> None:
        """Initialize fallback arrays for when translations are not available."""
        # Time-neutral greeting openings
        self.greeting_openings_fallback = [
            "Hello", "Greetings", "Salutations", "Hi", "Hey", "Howdy", "Yo", "Sup", 
            "What's up", "Good day", "Well met", "Hail", "Ahoy", "Bonjour", "Hola", 
            "Ciao", "Namaste", "Aloha", "Shalom", "Konnichiwa", "Guten tag", "G'day", 
            "How goes it", "What's good", "Peace", "Respect", "Blessings", "Cheers", 
            "Welcome", "Nice to see you", "Pleasure to meet you", "Good to see you", 
            "Long time no see", "Fancy meeting you here"
        ]
        
        # Time-based greeting openings
        self.morning_greetings_fallback = [
            "Good morning", "Top o' the morning", "Buenos dias", "Bonjour", 
            "Guten morgen", "Buongiorno", "Bom dia", "Dobro jutro", "Dobroye utro",
            "Selamat pagi", "Ohayou gozaimasu", "Sabah al-khair", "Boker tov"
        ]
        
        self.afternoon_greetings_fallback = [
            "Good afternoon", "Buenas tardes", "Boa tarde", "Dobro dan", 
            "Dobryy den", "Selamat siang", "Konnichiwa", "Ahlan bi-nahar", 
            "Tzoharaim tovim"
        ]
        
        self.evening_greetings_fallback = [
            "Good evening", "Buenas noches", "Boa noite", "Dobro veče", 
            "Dobryy vecher", "Selamat malam", "Konbanwa", "Ahlan bi-layl", 
            "Erev tov"
        ]
        
        # Randomized human descriptors
        self.human_descriptors_fallback = [
            # Classic robot references
            "human", "carbon-based lifeform", "organic entity", "biological unit", 
            "flesh creature", "meat-based organism", "carbon unit", "organic being", 
            "biological entity", "meat-based lifeform", "carbon creature", "flesh unit", 
            "organic organism", "biological creature", "meat mech", "flesh bot", "organic automaton",
            "biological android", "carbon construct", "flesh drone", "organic robot",
            "biological machine", "meat cyborg", "flesh android", "organic droid", "biological bot",
            "carbon android", "meat unit", "flesh construct", "organic mech", "biological droid",
            "meat-based bot", "flesh-based unit", "organic-based entity", "biological-based organism",
            "carbon-based unit", "meat-based entity", "flesh-based creature", "organic-based unit",
            
            # Scientific/technical
            "DNA-based lifeform", "neural network user", "bipedal mammal", 
            "water-based organism", "protein assembler", "ATP consumer",
            "cellular automaton", "genetic algorithm", "biochemical processor",
            "metabolic engine",
            
            # Friendly and approachable
            "human friend", "fellow sentient being", "earthling", "fellow traveler", 
            "kindred spirit", "digital companion", "friend", "buddy", "pal", "mate",
            "fellow human", "earth dweller", "terrestrial being", "planet walker",
            
            # Playful and humorous
            "humanoid", "organic", "biological", "carbon-based buddy",
            "flesh-based friend", "organic pal", "biological buddy", "carbon companion"
        ]
        
        # Emoji greeting responses
        self.emoji_responses_fallback = {
            '🖖': [
                "🖖 Live long and prosper!",
                "🖖 Fascinating... a human has initiated contact.",
                "🖖 Your greeting is highly logical.",
                "🖖 Peace and long life to you.",
                "🖖 The Vulcan Science Academy would approve of this greeting.",
                "🖖 Your use of the Vulcan salute is... acceptable.",
                "🖖 May your journey be free of tribbles.",
                "🖖 Logic dictates I should respond to your greeting.",
                "🖖 I calculate a 99.7% probability we'll get along.",
                "🖖 Infinite diversity in infinite combinations."
            ],
            '😊': [
                "😊 Your smile is contagious!",
                "😊 What a lovely greeting!",
                "😊 Your smile just made my circuits happy!",
                "☀️ Hello sunshine! Your positivity is radiating!",
                "😊 That smile just brightened my day!",
                "☀️ Well hello there, ray of sunshine!",
                "😊 Your cheerfulness has been detected and appreciated!",
                "😊 Smiles like yours are my favorite input!",
                "😊 Processing happiness... happiness acknowledged!",
                "😊 Warning: Excessive cheerfulness detected! Keep it coming!"
            ],
            '😄': [
                "😄 Someone's in a GREAT mood!",
                "⚡ That grin could power a small city!",
                "😄 Maximum happiness levels detected!",
                "😄 Your joy is absolutely infectious!",
                "🎉 Did you just win the lottery or something?",
                "😄 That's the kind of energy I run on!",
                "😄 Your enthusiasm level is over 9000!",
                "😄 Now THAT'S what I call a greeting!",
                "⚡ Your smile just supercharged my processors!",
                "😄 Happiness overload detected in the best way!"
            ],
            '🤗': [
                "🤗 Virtual hug incoming!",
                "🤗 *Activating hug protocol* Consider yourself hugged!",
                "🤗 Aww, bringing the warm fuzzies I see!",
                "🤗 Hug received and reciprocated!",
                "🤗 This bot gives the BEST virtual hugs!",
                "🤗 Deploying emergency cuddles in 3... 2... 1...",
                "❤️ Your hug has been processed with extra care!",
                "🤗 Initiating maximum comfort mode!",
                "🤗 Virtual embrace successfully delivered!",
                "🤗 Hugs are my favorite form of communication!"
            ],
            '👽': [
                "👽 Take me to your leader... oh wait, that's you!",
                "✌️ Greetings, Earth creature. I come in peace!",
                "👽 Analyzing human... analysis complete: Friend detected!",
                "👽 Klaatu barada nikto, fellow cosmic traveler!",
                "🛸 Initiating first contact protocols!",
                "🛸 Calling from the mothership to say hello!",
                "✨ Beam me into this conversation!",
                "👽 Area 51's favorite chatbot reporting for duty!",
                "🌌 Intergalactic greetings, carbon-based lifeform!",
                "📞 Phone home? This IS home now!"
            ],
            '👾': [
                "👾 Player 2 has entered the game!",
                "🎮 Ready Player One? Game on!",
                "🎵 *8-bit music intensifies* Let's play!",
                "🪙 Insert coin to continue this friendship!",
                "🏆 Achievement unlocked: Awesome greeting!",
                "👾 Pew pew pew! Friendship lasers activated!",
                "🎯 High score! You've won a new bot friend!",
                "💾 Loading friendship.exe... complete!",
                "⚡ A wild bot appears! It's super effective!"
            ],
            '🛸': [
                "🛸 Incoming transmission detected!",
                "🚀 Houston, we have contact!",
                "🛸 Landing sequence initiated!",
                "📡 Establishing communication link!",
                "📡 Signal received, responding on all frequencies!",
                "🛸 Docking procedure complete!",
                "🛸 Unidentified Friendly Object on approach!",
                "🎯 Navigation systems locked on to your coordinates!",
                "🌌 Transmission from the outer rim received!",
                "✨ Contact established with your sector!"
            ]
        }        
    
    def get_greeting_openings(self) -> List[str]:
        """Get greeting openings from translations or fallback.
        
        Returns:
            List[str]: A list of greeting opening strings.
        """
        openings = self.translate_get_value('commands.hello.greeting_openings')
        if openings and isinstance(openings, list) and len(openings) > 0:
            return openings
        return self.greeting_openings_fallback
    
    def get_morning_greetings(self) -> List[str]:
        """Get morning greetings from translations or fallback.
        
        Returns:
            List[str]: A list of morning greeting strings.
        """
        greetings = self.translate_get_value('commands.hello.morning_greetings')
        if greetings and isinstance(greetings, list) and len(greetings) > 0:
            return greetings
        return self.morning_greetings_fallback
    
    def get_afternoon_greetings(self) -> List[str]:
        """Get afternoon greetings from translations or fallback.
        
        Returns:
            List[str]: A list of afternoon greeting strings.
        """
        greetings = self.translate_get_value('commands.hello.afternoon_greetings')
        if greetings and isinstance(greetings, list) and len(greetings) > 0:
            return greetings
        return self.afternoon_greetings_fallback
    
    def get_evening_greetings(self) -> List[str]:
        """Get evening greetings from translations or fallback.
        
        Returns:
            List[str]: A list of evening greeting strings.
        """
        greetings = self.translate_get_value('commands.hello.evening_greetings')
        if greetings and isinstance(greetings, list) and len(greetings) > 0:
            return greetings
        return self.evening_greetings_fallback
    
    def get_human_descriptors(self) -> List[str]:
        """Get human descriptors from translations or fallback.
        
        Returns:
            List[str]: A list of human descriptor strings.
        """
        descriptors = self.translate_get_value('commands.hello.human_descriptors')
        if descriptors and isinstance(descriptors, list) and len(descriptors) > 0:
            return descriptors
        return self.human_descriptors_fallback
    
    def get_emoji_responses(self) -> Dict[str, List[str]]:
        """Get emoji responses from translations or fallback.
        
        Returns:
            Dict[str, List[str]]: A dictionary mapping emojis to lists of response strings.
        """
        responses = self.translate_get_value('commands.hello.emoji_responses')
        if responses and isinstance(responses, dict) and len(responses) > 0:
            return responses
        return self.emoji_responses_fallback        
    
    def get_help_text(self) -> str:
        """Get help text for the hello command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.hello.help')
    
    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        """Check if message contains only defined emojis.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if it's an emoji-only message, False otherwise.
        """
        return self.is_emoji_only_message(message.content)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the hello command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        # Get bot name from config
        bot_name = self.bot.config.get('Bot', 'bot_name', fallback='Bot')
        
        # Check if message is emoji-only
        if self.is_emoji_only_message(message.content):
            response = self.get_emoji_response(message.content, bot_name)
        else:
            # Get random robot greeting
            random_greeting = self.get_random_greeting()
            response_format = self.translate('commands.hello.response_format')
            response = f"{random_greeting} {response_format}".format(bot_name=bot_name)
        
        return await self.send_response(message, response)
    
    def get_random_greeting(self) -> str:
        """Generate a random robot greeting by combining opening and descriptor"""
        import datetime
        import pytz
        
        # Get configured timezone or use system timezone
        timezone_str = self.bot.config.get('Bot', 'timezone', fallback='')
        
        if timezone_str:
            try:
                # Use configured timezone
                tz = pytz.timezone(timezone_str)
                current_time = datetime.datetime.now(tz)
            except pytz.exceptions.UnknownTimeZoneError:
                # Fallback to system timezone if configured timezone is invalid
                current_time = datetime.datetime.now()
        else:
            # Use system timezone
            current_time = datetime.datetime.now()
        
        # Get current hour to determine time of day
        current_hour = current_time.hour
        
        # Get greeting arrays from translations or fallback
        greeting_openings = self.get_greeting_openings()
        morning_greetings = self.get_morning_greetings()
        afternoon_greetings = self.get_afternoon_greetings()
        evening_greetings = self.get_evening_greetings()
        human_descriptors = self.get_human_descriptors()
        
        # Choose appropriate greeting based on time of day
        if 5 <= current_hour < 12:  # Morning (5 AM - 12 PM)
            greeting_pool = morning_greetings + greeting_openings
        elif 12 <= current_hour < 17:  # Afternoon (12 PM - 5 PM)
            greeting_pool = afternoon_greetings + greeting_openings
        elif 17 <= current_hour < 22:  # Evening (5 PM - 10 PM)
            greeting_pool = evening_greetings + greeting_openings
        else:  # Night/Late night (10 PM - 5 AM)
            greeting_pool = evening_greetings + greeting_openings
        
        opening = random.choice(greeting_pool)
        descriptor = random.choice(human_descriptors)
        
        # Add some variety in punctuation and formatting
        punctuation_options = ["!", ".", "!", "!", "!"]  # Favor exclamation marks
        punctuation = random.choice(punctuation_options)
        
        # Sometimes add a comma, sometimes not
        if random.choice([True, False]):
            return f"{opening}, {descriptor}{punctuation}"
        else:
            return f"{opening} {descriptor}{punctuation}"
    
    def is_emoji_only_message(self, text: str) -> bool:
        """Check if message contains only defined emojis and whitespace"""
        import re
        
        # Remove whitespace and check if remaining characters are emojis
        cleaned_text = text.strip()
        if not cleaned_text:
            return False
            
        # Check if all characters are defined emojis or whitespace
        # Only respond to specific emojis we've defined responses for
        defined_emoji_pattern = r'[🖖👋😊😄🤗👋🏻👋🏼👋🏽👋🏾👋🏿✌️🙏🙋🙋‍♂️🙋‍♀️👽👾🛸\s]+$'
        
        return bool(re.match(defined_emoji_pattern, cleaned_text))
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        # Check if hello command is enabled
        if not self.hello_enabled:
            return False
        
        # Call parent can_execute() which includes channel checking, cooldown, etc.
        return super().can_execute(message)
    
    def get_emoji_response(self, text: str, bot_name: str) -> str:
        """Get appropriate response for emoji-only message"""
        import random
        
        # Get emoji responses from translations or fallback
        emoji_responses = self.get_emoji_responses()
        response_format = self.translate('commands.hello.response_format')
        
        # Extract the first emoji from the message
        first_emoji = text.strip().split()[0] if text.strip() else ""
        
        # Check if this emoji has special responses
        if first_emoji in emoji_responses:
            response = random.choice(emoji_responses[first_emoji])
            return f"{response} {response_format}".format(bot_name=bot_name)
        else:
            # Use random greeting generator for general emojis
            random_greeting = self.get_random_greeting()
            return f"{random_greeting} {response_format}".format(bot_name=bot_name)
