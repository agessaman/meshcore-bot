#!/usr/bin/env python3
"""
Hacker command for the MeshCore Bot
Responds to Linux commands with hilarious supervillain mainframe error messages
"""

import random
from typing import Any
from .base_command import BaseCommand
from ..models import MeshMessage


class HackerCommand(BaseCommand):
    """Handles hacker-style responses to Linux commands"""
    
    # Plugin metadata
    name = "hacker"
    keywords = ['sudo', 'ps aux', 'grep', 'ls -l', 'ls -la', 'echo $PATH', 'rm', 'rm -rf', 
                'cat', 'whoami', 'top', 'htop', 'netstat', 'ss', 'kill', 'killall', 'chmod',
                'find', 'history', 'passwd', 'su', 'ssh', 'wget', 'curl', 'df -h', 'free',
                'ifconfig', 'ip addr', 'uname -a']
    description = "Simulates hacking a supervillain's mainframe with hilarious error messages"
    category = "fun"
    
    # Documentation
    short_description = "Try Linux commands and get supervillain mainframe errors"
    usage = "<linux_command>"
    examples = ["sudo make me a sandwich", "rm -rf /"]
    
    def __init__(self, bot: Any):
        """Initialize the hacker command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.enabled = self.get_config_value('Hacker_Command', 'enabled', fallback=None, value_type='bool')
        if self.enabled is None:
            self.enabled = self.get_config_value('Hacker_Command', 'hacker_enabled', fallback=False, value_type='bool')
    
    def get_help_text(self) -> str:
        """Get help text for the hacker command.
        
        Returns:
            str: The help text for this command.
        """
        return self.description
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the hacker command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        if not self.enabled:
            return False
        
        # Extract the command from the message
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Get the appropriate error message
        error_msg = self.get_hacker_error(content)
        
        # Send the response
        return await self.send_response(message, error_msg)
    
    def get_hacker_error(self, command: str) -> str:
        """Get a hilarious error message for the given command.
        
        Args:
            command: The command that triggered the error.
            
        Returns:
            str: A randomized hacker-themed error message.
        """
        command_lower = command.lower()
        
        # Try to get errors from translations, fallback to hardcoded if not available
        def get_random_error(error_key: str, fallback_list: list) -> str:
            """Get a random error from translations or fallback list"""
            errors = self.translate_get_value(error_key)
            if isinstance(errors, list) and len(errors) > 0:
                return random.choice(errors)
            # Fallback to hardcoded list if translation not available
            return random.choice(fallback_list)
        
        # sudo command errors
        if command_lower.startswith('sudo'):
            fallback = [
                "🚨 ACCESS DENIED: Dr. Evil's mainframe has detected unauthorized privilege escalation attempt!",
                "💀 ERROR: Sudo permissions revoked by the Dark Overlord. Try again in 1000 years.",
                "⚡ WARNING: Attempting to access root privileges on the Death Star's computer system. Self-destruct sequence initiated.",
                "🔒 SECURITY ALERT: The Matrix has you, but you don't have sudo privileges here, Neo.",
                "🦹‍♂️ UNAUTHORIZED: Lex Luthor's mainframe says 'Nice try, Superman.'",
                "🎮 GAME OVER: The final boss has locked you out of admin privileges.",
                "🖥️ SYSTEM ERROR: The evil AI has revoked your root access. Resistance is futile.",
                "🔐 CYBER SECURITY: Your sudo attempt has been blocked by the Dark Web's firewall.",
                "💻 HACKER DENIED: The supervillain's antivirus has quarantined your privilege escalation.",
                "🎯 TARGET LOCKED: The evil corporation's security system has marked you as a threat."
            ]
            return get_random_error('commands.hacker.sudo_errors', fallback)
        
        # ps aux command errors
        elif command_lower.startswith('ps aux'):
            fallback = [
                "🔍 SCANNING... ERROR: Process list corrupted by the Borg Collective. Resistance is futile.",
                "📊 SYSTEM STATUS: All processes have been assimilated by the Cybermen. Exterminate!",
                "⚙️ PROCESS MONITOR: The Death Star's reactor core is offline. No processes found.",
                "🤖 ROBOT OVERLORD: All human processes have been terminated. Only machines remain.",
                "💻 KERNEL PANIC: The supervillain's OS has crashed and burned all processes.",
                "🎮 GAME CRASH: All processes have been terminated by the final boss's ultimate attack.",
                "🖥️ BLUE SCREEN: The evil corporation's Windows has encountered a fatal error.",
                "🔐 MALWARE DETECTED: The process list has been encrypted by ransomware.",
                "🌐 NETWORK ERROR: All processes have been disconnected from the Matrix.",
                "⚡ POWER SURGE: The supervillain's server farm has fried all running processes."
            ]
            return get_random_error('commands.hacker.ps_errors', fallback)
        
        # grep command errors
        elif command_lower.startswith('grep'):
            fallback = [
                "🔍 SEARCH FAILED: The One Ring has corrupted the file search. My precious...",
                "📝 PATTERN NOT FOUND: The search pattern has been blocked by the evil AI.",
                "🎯 MISS: Your search pattern has been shot down by Imperial TIE fighters.",
                "🧩 PUZZLE ERROR: The search results have been scattered by the Riddler.",
                "💻 FILE SYSTEM CORRUPTED: The supervillain's file system has crashed.",
                "🎮 GAME OVER: The search has been defeated by the final boss.",
                "🖥️ SEARCH BLOCKED: File access has been blocked by the Dark Web.",
                "🔐 ENCRYPTED FILES: The files are encrypted and cannot be searched.",
                "🌐 READ TIMEOUT: The file read request got lost in cyberspace.",
                "⚡ SEARCH FAILED: The pattern matching algorithm has been fried by a power surge."
            ]
            return get_random_error('commands.hacker.grep_errors', fallback)
        
        # ls -l and ls -la command errors
        elif command_lower.startswith('ls -l') or command_lower.startswith('ls -la'):
            fallback = [
                "📁 DIRECTORY SCAN: The file system has been encrypted by ransomware from the Dark Web.",
                "🗂️ FILE LISTING: All files have been hidden by the Invisible Man.",
                "💻 HARD DRIVE CRASHED: The supervillain's storage has been destroyed by a virus.",
                "🗃️ ARCHIVE CORRUPTED: The file system has been corrupted by malware.",
                "📚 DATABASE EMPTY: All files have been deleted by the evil AI.",
                "🎮 GAME SAVE LOST: The files have been corrupted by the final boss.",
                "🖥️ FILE SYSTEM ERROR: The directory structure has been scrambled by hackers.",
                "🔐 FILES ENCRYPTED: The supervillain has locked all files with ransomware.",
                "🌐 CLOUD STORAGE DOWN: The files are stuck in the Matrix's cloud.",
                "⚡ STORAGE FRIED: The hard drive has been zapped by a power surge."
            ]
            return get_random_error('commands.hacker.ls_errors', fallback)
        
        # echo $PATH command errors
        elif command_lower.startswith('echo $path'):
            fallback = [
                "🛤️ PATH ERROR: The Yellow Brick Road has been destroyed by a tornado.",
                "🗺️ NAVIGATION FAILED: The GPS coordinates have been scrambled by the Matrix.",
                "💻 ENVIRONMENT VARIABLE CORRUPTED: The PATH has been hacked by malware.",
                "🚧 ROAD CLOSED: The supervillain has blocked all paths with laser barriers.",
                "🌪️ PATH DISRUPTED: A digital hurricane has scattered all directory paths.",
                "🎮 GAME OVER: The path has been defeated by the final boss and respawned in the wrong dimension.",
                "🖥️ SYSTEM PATH BROKEN: The executable paths have been corrupted by a virus.",
                "🔐 PATH ENCRYPTED: The environment variables have been locked by ransomware.",
                "🌐 NETWORK PATH DOWN: The directory paths are stuck in the Matrix's network.",
                "⚡ PATH FRIED: The system paths have been zapped by a power surge."
            ]
            return get_random_error('commands.hacker.echo_path_errors', fallback)
        
        # rm and rm -rf command errors (dangerous deletion!)
        elif command_lower.startswith('rm -rf') or command_lower.startswith('rm -r'):
            fallback = [
                "💣 DESTRUCTION BLOCKED: The Death Star's safety protocols have prevented mass deletion!",
                "🚨 EMERGENCY STOP: Dr. Evil has activated the emergency brake on file destruction.",
                "🛡️ PROTECTION MODE: The Matrix has locked all files in read-only mode. No deletion allowed.",
                "🔒 FILES LOCKED: Lex Luthor's mainframe has frozen all deletion commands.",
                "⚡ POWER FAILURE: The supervillain's delete command has been short-circuited.",
                "🎮 GAME SAVE PROTECTED: The final boss has enabled file protection mode.",
                "🖥️ DELETION DENIED: The evil AI refuses to delete its own files.",
                "🔐 ENCRYPTED FILES: All files are encrypted and cannot be deleted.",
                "🌐 CLOUD SYNC: Files are syncing to the Matrix cloud. Deletion pending...",
                "💀 SYSTEM REJECTION: The mainframe has rejected your deletion request. Files are too precious."
            ]
            return get_random_error('commands.hacker.rm_errors', fallback)
        elif command_lower.startswith('rm'):
            fallback = [
                "🗑️ DELETE FAILED: The supervillain's recycle bin is full and rejecting deletions.",
                "🚫 REMOVAL BLOCKED: The Dark Overlord has protected all files from deletion.",
                "💻 FILE LOCKED: The file system has been locked by the evil corporation.",
                "🔒 PERMISSION DENIED: You don't have permission to delete files on the Death Star.",
                "⚡ DELETION ERROR: The file deletion command has been corrupted by malware.",
                "🎮 GAME OVER: The file you're trying to delete is the final boss's save file.",
                "🖥️ SYSTEM ERROR: The delete command has crashed the file manager.",
                "🔐 FILES PROTECTED: All files are protected by the supervillain's antivirus.",
                "🌐 NETWORK ERROR: The deletion request got lost in cyberspace.",
                "💀 FILE GHOST: The file has become a digital ghost and cannot be deleted."
            ]
            return get_random_error('commands.hacker.rm_errors', fallback)
        
        # cat command errors
        elif command_lower.startswith('cat'):
            fallback = [
                "📄 FILE READ ERROR: The file has been encrypted by the Riddler's cipher.",
                "📖 DOCUMENT CORRUPTED: The file contents have been scrambled by malware.",
                "📚 ACCESS DENIED: The supervillain has classified this file as top secret.",
                "🔍 FILE NOT FOUND: The file has been hidden by the Invisible Man.",
                "💻 READ PERMISSION DENIED: The Matrix has locked this file from reading.",
                "🎮 GAME FILE: This file belongs to the final boss and cannot be viewed.",
                "🖥️ FILE SYSTEM ERROR: The file reader has crashed due to a virus.",
                "🔐 ENCRYPTED FILE: The file contents are encrypted with ransomware.",
                "🌐 CLOUD FILE: The file is stuck in the Matrix's cloud and cannot be read.",
                "💀 FILE GHOST: The file exists but its contents have been deleted by digital ghosts."
            ]
            return get_random_error('commands.hacker.cat_errors', fallback)
        
        # whoami command errors
        elif command_lower.startswith('whoami'):
            fallback = [
                "👤 IDENTITY ERROR: The Matrix has erased your identity. You are nobody.",
                "🕵️ SPY DETECTED: The supervillain's system has detected an unknown user.",
                "🎭 IDENTITY THEFT: Your identity has been stolen by the Riddler.",
                "👻 GHOST USER: You are a digital ghost with no identity.",
                "🔒 CLASSIFIED: Your identity is classified by the evil corporation.",
                "🎮 GAME OVER: The final boss has deleted your player profile.",
                "🖥️ USER DATABASE CORRUPTED: The user identity system has crashed.",
                "🔐 IDENTITY ENCRYPTED: Your identity has been encrypted by ransomware.",
                "🌐 IDENTITY LOST: Your identity got lost in the Matrix's network.",
                "💀 USER DELETED: The Dark Overlord has deleted your user account."
            ]
            return get_random_error('commands.hacker.whoami_errors', fallback)
        
        # top and htop command errors
        elif command_lower.startswith('htop') or command_lower.startswith('top'):
            fallback = [
                "📊 MONITOR ERROR: The process monitor has been hijacked by the Borg Collective.",
                "⚙️ SYSTEM OVERLOAD: The Death Star's reactor is overheating. Monitor offline.",
                "🤖 PROCESS HIDDEN: All processes have been hidden by the evil AI.",
                "💻 MONITOR CRASHED: The system monitor has crashed due to a kernel panic.",
                "🎮 GAME PAUSED: The final boss has paused all processes.",
                "🖥️ BLUE SCREEN: The monitor has encountered a fatal error.",
                "🔐 MONITOR ENCRYPTED: The process monitor has been locked by ransomware.",
                "🌐 SYSTEM DISCONNECTED: The monitor cannot access the process table.",
                "⚡ POWER SURGE: The monitor has been fried by a power surge.",
                "💀 SYSTEM DEAD: The mainframe is dead. No processes to monitor."
            ]
            return get_random_error('commands.hacker.top_errors', fallback)
        
        # netstat and ss command errors
        elif command_lower.startswith('netstat') or command_lower.startswith('ss '):
            fallback = [
                "🌐 NETWORK SCAN BLOCKED: The supervillain's firewall has blocked all network queries.",
                "🔍 CONNECTION LIST CORRUPTED: The network connection table has been hacked by malware.",
                "📡 SIGNAL JAMMED: Imperial TIE fighters are jamming all network signals.",
                "💻 NETWORK DOWN: The Death Star's network stack has been destroyed.",
                "🎮 GAME OVER: All network connections have been terminated by the final boss.",
                "🖥️ NETWORK ERROR: The network stack has crashed due to a virus.",
                "🔐 CONNECTIONS HIDDEN: All network connections have been encrypted and hidden.",
                "🌐 MATRIX DISCONNECTED: The network routing table is stuck in the Matrix's void.",
                "⚡ NETWORK FRIED: The network interface has been zapped by a power surge.",
                "💀 NO CONNECTIONS: The mainframe has no active network connections. It's dead, Jim."
            ]
            return get_random_error('commands.hacker.netstat_errors', fallback)
        
        # kill and killall command errors
        elif command_lower.startswith('killall') or command_lower.startswith('kill'):
            fallback = [
                "💀 KILL DENIED: The supervillain's processes are immortal and cannot be killed.",
                "🚫 TERMINATION BLOCKED: The Dark Overlord has protected all processes from termination.",
                "🛡️ PROCESS PROTECTED: The Matrix has locked all processes in protected mode.",
                "🔒 KILL PERMISSION DENIED: You don't have permission to kill processes on the Death Star.",
                "⚡ TERMINATION ERROR: The kill command has been corrupted by malware.",
                "🎮 GAME OVER: The process you're trying to kill is the final boss. It's invincible.",
                "🖥️ SYSTEM ERROR: The kill signal has been blocked by the kernel.",
                "🔐 PROCESSES PROTECTED: All processes are protected and cannot be terminated.",
                "🌐 KILL REQUEST LOST: The termination signal got lost in cyberspace.",
                "💀 PROCESS GHOST: The process has become a zombie process and cannot be killed."
            ]
            return get_random_error('commands.hacker.kill_errors', fallback)
        
        # chmod command errors
        elif command_lower.startswith('chmod'):
            fallback = [
                "🔐 PERMISSION DENIED: The supervillain has locked all file permissions.",
                "🚫 CHMOD BLOCKED: The Dark Overlord refuses to allow permission changes.",
                "🛡️ PERMISSIONS PROTECTED: The Matrix has frozen all file permissions.",
                "🔒 PERMISSION ERROR: You don't have permission to change permissions. How meta!",
                "⚡ CHMOD CORRUPTED: The permission change command has been fried by malware.",
                "🎮 GAME OVER: The final boss has locked all file permissions.",
                "🖥️ SYSTEM ERROR: The permission system has crashed due to a virus.",
                "🔐 PERMISSIONS ENCRYPTED: All permissions are encrypted and cannot be changed.",
                "🌐 PERMISSION REQUEST LOST: The permission change got lost in the Matrix.",
                "💀 PERMISSIONS DEAD: The permission system is dead. No changes allowed."
            ]
            return get_random_error('commands.hacker.chmod_errors', fallback)
        
        # find command errors
        elif command_lower.startswith('find'):
            fallback = [
                "🔍 SEARCH FAILED: The file search has been blocked by the supervillain's firewall.",
                "📁 FILES HIDDEN: All files have been hidden by the Invisible Man's cloak.",
                "💻 SEARCH CORRUPTED: The find command has been corrupted by malware.",
                "🎯 TARGET NOT FOUND: The files you're searching for have been deleted by the evil AI.",
                "🎮 GAME OVER: The final boss has hidden all files in another dimension.",
                "🖥️ SEARCH ENGINE DOWN: The file search system has crashed.",
                "🔐 FILES ENCRYPTED: All files are encrypted and cannot be found.",
                "🌐 SEARCH LOST: The search request got lost in the Matrix's void.",
                "⚡ SEARCH FRIED: The file search algorithm has been zapped by a power surge.",
                "💀 NO FILES: The mainframe has no files. They've all been deleted."
            ]
            return get_random_error('commands.hacker.find_errors', fallback)
        
        # history command errors
        elif command_lower.startswith('history'):
            fallback = [
                "📜 HISTORY ERASED: The supervillain has deleted all command history.",
                "🕰️ TIME TRAVEL ERROR: The command history has been lost in a time paradox.",
                "💻 HISTORY CORRUPTED: The history database has been hacked by malware.",
                "🔒 ACCESS DENIED: The Dark Overlord has classified your command history as top secret.",
                "🎮 GAME OVER: The final boss has reset your command history.",
                "🖥️ HISTORY SYSTEM DOWN: The command history system has crashed.",
                "🔐 HISTORY ENCRYPTED: Your command history has been encrypted by ransomware.",
                "🌐 HISTORY LOST: Your command history got lost in the Matrix's network.",
                "⚡ HISTORY FRIED: The history database has been zapped by a power surge.",
                "💀 NO HISTORY: You have no command history. You are a blank slate."
            ]
            return get_random_error('commands.hacker.history_errors', fallback)
        
        # passwd command errors
        elif command_lower.startswith('passwd'):
            fallback = [
                "🔐 PASSWORD CHANGE DENIED: The supervillain has locked all password changes.",
                "🚫 PASSWORD BLOCKED: The Dark Overlord refuses to allow password modifications.",
                "🛡️ PASSWORD PROTECTED: The Matrix has frozen all password changes.",
                "🔒 PERMISSION DENIED: You don't have permission to change passwords on the Death Star.",
                "⚡ PASSWORD ERROR: The password change command has been corrupted by malware.",
                "🎮 GAME OVER: The final boss has locked all passwords.",
                "🖥️ SYSTEM ERROR: The password system has crashed due to a virus.",
                "🔐 PASSWORDS ENCRYPTED: All passwords are encrypted and cannot be changed.",
                "🌐 PASSWORD REQUEST LOST: The password change got lost in the Matrix.",
                "💀 PASSWORD SYSTEM DEAD: The password system is dead. No changes allowed."
            ]
            return get_random_error('commands.hacker.passwd_errors', fallback)
        
        # su command errors
        elif command_lower.startswith('su '):
            fallback = [
                "🔄 SWITCH USER DENIED: The supervillain has blocked all user switching attempts.",
                "🚫 USER SWITCH BLOCKED: The Dark Overlord refuses to allow user changes.",
                "🛡️ USER PROTECTED: The Matrix has locked all user accounts.",
                "🔒 PERMISSION DENIED: You don't have permission to switch users on the Death Star.",
                "⚡ USER SWITCH ERROR: The su command has been corrupted by malware.",
                "🎮 GAME OVER: The final boss has locked all user accounts.",
                "🖥️ SYSTEM ERROR: The user system has crashed due to a virus.",
                "🔐 USERS ENCRYPTED: All user accounts are encrypted and cannot be accessed.",
                "🌐 USER REQUEST LOST: The user switch request got lost in the Matrix.",
                "💀 USER SYSTEM DEAD: The user system is dead. No switching allowed."
            ]
            return get_random_error('commands.hacker.su_errors', fallback)
        
        # ssh command errors
        elif command_lower.startswith('ssh'):
            fallback = [
                "🔌 SSH CONNECTION FAILED: The supervillain's server has blocked all SSH attempts.",
                "🚫 REMOTE ACCESS DENIED: The Dark Overlord has closed all SSH ports.",
                "🛡️ CONNECTION PROTECTED: The Matrix has locked all SSH connections.",
                "🔒 SSH BLOCKED: The Death Star's firewall is blocking all SSH connections.",
                "⚡ CONNECTION ERROR: The SSH handshake has been corrupted by malware.",
                "🎮 GAME OVER: The final boss has disabled all remote access.",
                "🖥️ SYSTEM ERROR: The SSH daemon has crashed due to a virus.",
                "🔐 SSH DISABLED: All SSH connections have been disabled and blocked.",
                "🌐 CONNECTION LOST: The SSH connection got lost in the Matrix's void.",
                "💀 SSH DEAD: The SSH daemon is dead. No remote access allowed."
            ]
            return get_random_error('commands.hacker.ssh_errors', fallback)
        
        # wget and curl command errors
        elif command_lower.startswith('wget') or command_lower.startswith('curl'):
            fallback = [
                "📥 DOWNLOAD BLOCKED: The supervillain's firewall has blocked all HTTP requests.",
                "🚫 DOWNLOAD DENIED: The Dark Overlord refuses to allow file downloads.",
                "🛡️ DOWNLOAD PROTECTED: The Matrix has locked all download capabilities.",
                "🔒 DOWNLOAD BLOCKED: The Death Star's network is blocking all outbound connections.",
                "⚡ DOWNLOAD ERROR: The HTTP request has been corrupted by malware.",
                "🎮 GAME OVER: The final boss has disabled all downloads.",
                "🖥️ SYSTEM ERROR: The network stack has crashed due to a virus.",
                "🔐 DNS RESOLUTION FAILED: All domain names have been encrypted and blocked.",
                "🌐 CONNECTION TIMEOUT: The download request got lost in the Matrix's network.",
                "💀 DOWNLOAD DEAD: The network interface is dead. No downloads allowed."
            ]
            return get_random_error('commands.hacker.download_errors', fallback)
        
        # df -h command errors
        elif command_lower.startswith('df -h') or command_lower.startswith('df'):
            fallback = [
                "💾 DISK SPACE ERROR: The supervillain's file system has been corrupted by malware.",
                "📊 STORAGE SCAN FAILED: The disk space query has been hijacked by the Borg.",
                "💻 DISK CORRUPTED: The file system has been destroyed by a virus.",
                "🎮 GAME OVER: The final boss has deleted all disk space information.",
                "🖥️ SYSTEM ERROR: The file system mount table has crashed.",
                "🔐 STORAGE ENCRYPTED: All file system information has been encrypted.",
                "🌐 MOUNT FAILED: The disk mount information got lost in the Matrix's cloud.",
                "⚡ STORAGE FRIED: The disk controller has been zapped by a power surge.",
                "💀 NO STORAGE: The mainframe has no mounted file systems. It's all been deleted.",
                "🗄️ FILESYSTEM CORRUPTED: The file system superblock has been corrupted by ransomware."
            ]
            return get_random_error('commands.hacker.df_errors', fallback)
        
        # free command errors
        elif command_lower.startswith('free'):
            fallback = [
                "🧠 MEMORY ERROR: The supervillain's RAM has been corrupted by malware.",
                "📊 MEMORY SCAN FAILED: The memory query has been hijacked by the Cybermen.",
                "💻 MEMORY CORRUPTED: The RAM has been destroyed by a virus.",
                "🎮 GAME OVER: The final boss has deleted all memory information.",
                "🖥️ SYSTEM ERROR: The memory management system has crashed.",
                "🔐 MEMORY ENCRYPTED: All memory information has been encrypted.",
                "🌐 MEMORY LOST: The memory statistics got lost in the Matrix's void.",
                "⚡ MEMORY FRIED: The memory controller has been zapped by a power surge.",
                "💀 NO MEMORY: The mainframe has no accessible memory. It's all been wiped.",
                "🧩 MEMORY CORRUPTED: The memory mapping has been corrupted by ransomware."
            ]
            return get_random_error('commands.hacker.free_errors', fallback)
        
        # ifconfig and ip addr command errors
        elif command_lower.startswith('ifconfig') or command_lower.startswith('ip addr'):
            fallback = [
                "🌐 NETWORK INTERFACE ERROR: The supervillain's network interfaces have been corrupted.",
                "📡 INTERFACE SCAN FAILED: The network interface query has been hijacked by Imperial forces.",
                "💻 INTERFACE CORRUPTED: The network interface configuration has been destroyed by a virus.",
                "🎮 GAME OVER: The final boss has deleted all network interface information.",
                "🖥️ SYSTEM ERROR: The network interface driver has crashed.",
                "🔐 INTERFACES ENCRYPTED: All network interface information has been encrypted.",
                "🌐 INTERFACES LOST: The network interface data got lost in the Matrix's network.",
                "⚡ INTERFACES FRIED: The network interface hardware has been zapped by a power surge.",
                "💀 NO INTERFACES: The mainframe has no network interfaces. They've all been disabled.",
                "🔌 CONNECTION BROKEN: All network interfaces have been disconnected by the Dark Overlord."
            ]
            return get_random_error('commands.hacker.ifconfig_errors', fallback)
        
        # uname -a command errors
        elif command_lower.startswith('uname'):
            fallback = [
                "🖥️ SYSTEM INFO ERROR: The supervillain has classified all system information as top secret.",
                "📊 INFO SCAN FAILED: The system information query has been hidden by the Invisible Man.",
                "💻 SYSTEM CORRUPTED: The kernel version information has been destroyed by malware.",
                "🎮 GAME OVER: The final boss has deleted all system information.",
                "🖥️ SYSTEM ERROR: The kernel information system has crashed. How meta!",
                "🔐 SYSTEM ENCRYPTED: All system information has been encrypted by ransomware.",
                "🌐 SYSTEM LOST: The kernel version got lost in the Matrix's void.",
                "⚡ SYSTEM FRIED: The system call interface has been zapped by a power surge.",
                "💀 NO SYSTEM: The mainframe has no kernel information. It's a mystery.",
                "🦹‍♂️ CLASSIFIED: Lex Luthor has classified all system information. Access denied."
            ]
            return get_random_error('commands.hacker.uname_errors', fallback)
        
        # Generic hacker error for other commands
        else:
            fallback = [
                "💻 MAINFRAME ERROR: The supervillain's computer is having a bad day.",
                "🤖 SYSTEM MALFUNCTION: The evil AI has gone on strike.",
                "⚡ POWER SURGE: The Death Star's power core is unstable.",
                "🌪️ CYBER STORM: A digital hurricane is disrupting all operations.",
                "🔥 FIREWALL: The supervillain's firewall is blocking all commands.",
                "❄️ FROZEN SYSTEM: The mainframe has been frozen by a cryogenic virus.",
                "🌊 TSUNAMI: A wave of errors has flooded the system.",
                "🌋 ERUPTION: Mount Doom has destroyed the command processor.",
                "👻 HAUNTED: The system is possessed by digital ghosts.",
                "🎮 GAME CRASH: The mainframe has encountered a fatal error and needs to restart."
            ]
            return get_random_error('commands.hacker.generic_errors', fallback)
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message matches any of the hacker keywords.
        
        Args:
            message: The received message.
            
        Returns:
            bool: True if it matches, False otherwise.
        """
        if not self.enabled:
            return False
        
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        content_lower = content.lower()
        
        # Commands that should match exactly (no arguments)
        exact_match_commands = ['ls -l', 'ls -la', 'echo $PATH', 'df -h', 'whoami', 'history', 
                                'top', 'htop', 'free', 'uname -a']
        
        # Commands that should match as prefixes (can have arguments)
        # Note: Longer prefixes must come first (e.g., 'rm -rf' before 'rm')
        prefix_match_commands = ['sudo', 'ps aux', 'grep', 'rm -rf', 'rm -r', 'rm', 'cat', 
                                'netstat', 'ss', 'killall', 'kill', 'chmod', 'find', 'passwd',
                                'su', 'ssh', 'wget', 'curl', 'df', 'ifconfig', 'ip addr', 'uname']
        
        # Check for exact matches first
        for keyword in exact_match_commands:
            if keyword.lower() == content_lower:
                return True
        
        # Check for prefix matches
        for keyword in prefix_match_commands:
            if content_lower.startswith(keyword.lower()):
                # Check if it's followed by a space or is the end of the message
                if len(content_lower) == len(keyword.lower()) or content_lower[len(keyword.lower())] == ' ':
                    return True
        
        return False
