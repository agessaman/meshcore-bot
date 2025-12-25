#!/usr/bin/env python3
"""
Magic 8-ball command for the MeshCore Bot
Handles the 'magic8' keyword response
"""
import random
from .base_command import BaseCommand
from ..models import MeshMessage

magic8_responses = ["It is certain.","It is decidedly so.","Without a doubt.","Yes definitely.","You may rely on it.","As I see it, yes.","Most likely.","Outlook good.","Yes.","Signs point to yes.","Reply hazy, try again.","Ask again later.","Better not tell you now.","Cannot predict now.","Concentrate and ask again.","Don't count on it.","My reply is no.","My sources say no.","Outlook not so good.","Very doubtful."]

def magic8():
    answer=magic8_responses[random.randint(0,len(magic8_responses)-1)]
    return answer


class Magic8Command(BaseCommand):
    """Handles the magic8 command"""
    
    # Plugin metadata
    name = "magic8"
    keywords = ['magic8']
    description = "Emulates the classic Magic 8-ball toy'"
    category = "games"
    
    def get_help_text(self) -> str:
        return self.translate('commands.magic8.description')
    
    def get_response_format(self) -> str:
        """Get the response format from config"""
        if self.bot.config.has_section('Keywords'):
            format_str = self.bot.config.get('Keywords', 'magic8', fallback=None)
            return self._strip_quotes_from_config(format_str) if format_str else None
        return None
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the magic8 command"""
        answer = magic8()
        return await self.send_response(message, answer)
