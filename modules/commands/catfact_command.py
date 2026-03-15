#!/usr/bin/env python3
"""
Cat Fact command for the MeshCore Bot
Provides random cat facts as a hidden easter egg command
"""

import random

from ..models import MeshMessage
from .base_command import BaseCommand


class CatfactCommand(BaseCommand):
    """Handles cat fact commands - hidden easter egg.

    Responds to various cat-related keywords with random facts about cats.
    This is designed as a hidden feature and does not appear in standard help listings.
    """

    # Plugin metadata
    name = "catfact"
    keywords = ['catfact', 'cat', 'meow', 'purr', 'kitten']
    description = "Get a random cat fact (hidden command)"
    category = "hidden"  # Hidden category so it won't appear in help
    cooldown_seconds = 3  # 3 second cooldown per user

    def __init__(self, bot):
        """Initialize the catfact command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.catfact_enabled = self.get_config_value('Catfact_Command', 'enabled', fallback=True, value_type='bool')

        # Collection of cat facts - fallback if translations not available
        self.cat_facts_fallback = [
            "Cats have a third eyelid called a nictitating membrane that protects and moistens their eyes. 🐱",
            "A group of cats is called a 'clowder' or a 'glaring'. 🐈",
            "Cats can rotate their ears 180 degrees independently to pinpoint sounds. 👂",
            "The oldest known pet cat existed 9,500 years ago in Cyprus. 🏺",
            "Cats have 32 muscles in each ear, while humans have only 6. 🎧",
            "A cat's purr vibrates at 25-150 Hz, which can promote healing of bones and tissues. 🩹",
            "Cats sleep 12-18 hours per day - that's 50-70% of their lives! 😴",
            "A cat's nose print is unique, just like human fingerprints. 👃",
            "Cats can't taste sweetness due to a missing taste receptor gene. 🍭",
            "Blackie the cat inherited £7 million ($12.5 million) in 1988. 💰",
            "Cats have free-floating clavicles that give them extreme flexibility. 🦴",
            "A cat's heart beats 140-220 times per minute, about twice as fast as a human's. ❤️",
            "Cats can survive falls from over 20 stories due to their righting reflex. 🏢",
            "The technical term for a cat's hairball is a 'trichobezoar'. 🤮",
            "Cats can jump 5-6 times their body length in a single bound. 🦘",
            "A cat's whiskers are as wide as their body, helping them judge if they can fit through spaces. 📏",
            "Cats have 32 muscles in each ear to detect sounds and move ears independently. 🎯",
            "The oldest cat ever lived to 38 years and 3 days (Creme Puff, Texas). 🎂",
            "Cats can run up to 30 mph in short bursts. 🏃‍♂️",
            "Cat brains are 90% structurally similar to human brains. 🧠",
            "Cats have Jacobson's organ in the roof of their mouth that lets them 'taste' scents. 👅",
            "Félicette was the first cat in space, launched by France in 1963. 🚀",
            "Cats need only 1/6th the light humans need to see clearly in the dark. 🌙",
            "A cat's tail contains nearly 10% of all the bones in its body. 🦴",
            "The world's longest cat measured 48.5 inches from nose to tail (Stewie, Maine Coon). 📏",
            "Cats can make over 100 different vocalizations, while dogs make about 10. 🎵",
            "A cat's sense of smell is 14 times stronger than a human's. 👃",
            "Cats have a 'flehmen response' where they curl their lip to better detect scents. 😬",
            "The first major cat show was held in London in 1871 at Crystal Palace. 🏆",
            "Cats can drink seawater to survive - their kidneys filter out the salt efficiently. 🌊",
            "A cat's purr can help lower blood pressure and reduce stress in humans. 🧘",
            "Cats can travel hundreds of miles home using their magnetic field sensitivity. 🗺️",
            "The smallest cat breed is the Singapura, weighing only 4-8 pounds. ⚖️",
            "Cats can see ultraviolet light that humans cannot see. 🌈",
            "A cat's tongue is covered in 290-300 tiny backward-facing hooks called papillae. 🪝",
            "Ancient Egyptians considered cats sacred vessels for the goddess Bastet. 👑",
            "Taylor Swift's cat Olivia Benson has a net worth of $97 million. 💎",
            "Cats have 230 bones in their body - 24 more than humans have. 🦴",
            "Cats can hear frequencies up to 64,000 Hz, while humans max out at 20,000 Hz. 🎧",
            "Taylor Swift's cat Benjamin Button appeared on her TIME Person of the Year cover. 📰",
            "Taylor Swift's cats are named Meredith Grey, Olivia Benson, and Benjamin Button. 🎸",
            "Cats walk like camels and giraffes, moving both right legs then both left legs. 🐾",
            "The ancient Egyptian word for cat was 'Miu' or 'Mau' - sounding like a meow! 📜",
            "Cat whiskers have nerve endings as sensitive as human fingertips. 🎯",
            "Only domestic cats walk with their tails held high as a sign of trust and happiness. 🐈",
            "Cats have 250 million neurons in their cerebral cortex - more than dogs have. 🧠",
            "Cat purrs vibrate at the same frequency as bone-healing medical devices. 💊",
            "Taylor Swift's cat Olivia has earned millions from appearing in music videos and ads. 💸",
            "Killing a cat in ancient Egypt was punishable by death. ⚖️",
            "Taylor Swift named her home recording studio 'The Itty Bitty Kitty Committee'. 🎤",
            "Taylor Swift's cat Olivia Benson is the official logo for Taylor Swift Productions. 📺",
            "Ed Sheeran bought Scottish Fold cats after being inspired by Taylor Swift's cats. 🎶",
            "Taylor Swift's cats have their own IMDB pages with acting credits. 🎬",
            "Mariska Hargitay named her cat 'Karma' after Taylor Swift's song. 💕",
            "Cats have dewclaws on their front paws that work like thumbs for gripping. 🐾",
            "Cat pupils can expand to 50% larger than human pupils to capture more light. 👁️",
            "Cats have 30 adult teeth compared to humans' 32. 🦷",
            "Cats can taste ATP (energy molecules), which signals fresh meat to them. 😋",
            "Cats have whiskers on the backs of their front legs to detect prey movement. 🦵",
            "The Egyptian Mau is the fastest domestic cat breed at 30 mph. 🏃",
            "A Nobel Prize was awarded in 1981 for research using cat vision studies. 🏅",
            "Cats are digitigrade, meaning they walk on their toes, not flat-footed. 🦶",
            "Cats can filter salt from seawater - an adaptation from their desert-dwelling ancestors. 🏜️",
            "Cats were domesticated around 10,000-12,000 years ago in the Near East. 🌍",
            "Benjamin Button is the first and only cat to ever appear on TIME Person of the Year cover. 📸",
            "A cat's flexible spine allows them to rotate their body mid-air when falling. 🤸",
            "Cats spend about 30-50% of their day grooming themselves and other cats. 🛁",
            "A cat's average body temperature is 101.5°F (38.6°C) - higher than humans. 🌡️"
        ]

    def get_cat_facts(self) -> list[str]:
        """Get cat facts from translations or fallback to hardcoded list.

        Returns:
            List[str]: A list of cat fact strings.
        """
        facts = self.translate_get_value('commands.catfact.facts')
        if facts and isinstance(facts, list) and len(facts) > 0:
            return facts
        return self.cat_facts_fallback

    def get_help_text(self) -> str:
        """Get help text for the catfact command.

        Returns:
            str: Empty string (to keep the command hidden).
        """
        # Return empty string so it doesn't appear in help
        return ""

    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.catfact_enabled:
            return False
        return super().can_execute(message)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the cat fact command.

        Selects a random cat fact and sends it to the user.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Get cat facts from translations or fallback
            cat_facts = self.get_cat_facts()

            # Get a random cat fact
            cat_fact = random.choice(cat_facts)

            # Send the cat fact
            await self.send_response(message, cat_fact)
            return True

        except Exception as e:
            self.logger.error(f"Error in cat fact command: {e}")
            await self.send_response(message, self.translate('commands.catfact.error'))
            return True
