#!/usr/bin/env python3
"""
Repeater Management Command
Provides commands to manage repeater contacts and purging operations
"""

import asyncio
from .base_command import BaseCommand
from ..models import MeshMessage
from typing import List, Optional


class RepeaterCommand(BaseCommand):
    """Command for managing repeater contacts.

    Provides functionality to scan, list, purge, and manage repeater and companion contacts
    within the mesh network. Includes automated cleanup tools and statistics.
    """

    # Plugin metadata
    name = "repeater"
    keywords = ["repeater", "repeaters", "rp"]
    description = "Manage repeater contacts and purging operations (DM only)"
    requires_dm = True
    cooldown_seconds = 0
    category = "management"
    requires_internet = True  # Requires internet access for geocoding (Nominatim)

    # LoRa message size limit (conservative to avoid overload)
    # DM limit is 150 chars, public channel is 237 chars
    MAX_LORA_MESSAGE_SIZE = 140  # characters, leaves buffer for protocol overhead

    def __init__(self, bot):
        super().__init__(bot)
        self.repeater_enabled = self.get_config_value('Repeater_Command', 'enabled', fallback=True, value_type='bool')

    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.repeater_enabled:
            return False
        return super().can_execute(message)

    def _truncate_for_lora(self, message: str, max_size: int = None) -> str:
        """Truncate message to fit within LoRa size limits.

        Args:
            message: The message to truncate
            max_size: Maximum size (defaults to MAX_LORA_MESSAGE_SIZE)

        Returns:
            Truncated message with indicator if truncated
        """
        max_size = max_size or self.MAX_LORA_MESSAGE_SIZE
        if len(message) <= max_size:
            return message

        truncate_suffix = "...(use web viewer)"
        available_size = max_size - len(truncate_suffix)
        return message[:available_size] + truncate_suffix

    def _get_deprecation_warning(self, web_viewer_url: str = None) -> str:
        """Get deprecation warning message for commands replaced by web viewer.

        Args:
            web_viewer_url: Optional custom web viewer URL

        Returns:
            Deprecation warning message
        """
        if web_viewer_url:
            return f"⚠️ DEPRECATED: Use web viewer at {web_viewer_url}/contacts"
        return "⚠️ DEPRECATED: Use web viewer for this feature (check bot config for URL)"
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with 'repeater' keyword.
        
        Args:
            message: The message to check for the keyword.
            
        Returns:
            bool: True if the message starts with any of the command keywords.
        """
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Check if message starts with any of our keywords
        content_lower = content.lower()
        for keyword in self.keywords:
            if content_lower.startswith(keyword + ' '):
                return True
        return False
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute repeater management command.
        
        Parses subcommands (scan, list, purge, etc.) and routes to the appropriate handler.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        self.logger.info(f"Repeater command executed with content: {message.content}")
        
        # Parse the message content to extract subcommand and args
        content = message.content.strip()
        parts = content.split()
        
        if len(parts) < 2:
            response = self.get_help()
        else:
            subcommand = parts[1].lower()
            args = parts[2:] if len(parts) > 2 else []
            
            try:
                if subcommand == "scan":
                    response = await self._handle_scan()
                elif subcommand == "list":
                    response = await self._handle_list(args)
                elif subcommand == "purge":
                    response = await self._handle_purge(args)
                elif subcommand == "restore":
                    response = await self._handle_restore(args)
                elif subcommand == "stats":
                    response = await self._handle_stats()
                elif subcommand == "status":
                    response = await self._handle_status()
                elif subcommand == "manage":
                    response = await self._handle_manage(args)
                elif subcommand == "add":
                    response = await self._handle_add(args)
                elif subcommand == "discover":
                    response = await self._handle_discover()
                elif subcommand == "auto":
                    response = await self._handle_auto(args)
                elif subcommand == "tst":
                    response = await self._handle_test(args)
                elif subcommand == "locations":
                    response = await self._handle_locations()
                elif subcommand == "update-geo":
                    dry_run = "dry-run" in args
                    batch_size = 10  # Default batch size
                    # Look for batch size argument
                    for i, arg in enumerate(args):
                        if arg.isdigit():
                            batch_size = int(arg)
                            break
                    response = await self._handle_update_geolocation(dry_run, batch_size)
                elif subcommand == "auto-purge":
                    response = await self._handle_auto_purge(args)
                elif subcommand == "purge-status":
                    response = await self._handle_purge_status()
                elif subcommand == "test-purge":
                    response = await self._handle_test_purge()
                elif subcommand == "debug-purge":
                    response = await self._handle_debug_purge()
                elif subcommand == "geocode":
                    response = await self._handle_geocode(args)
                elif subcommand == "help":
                    response = self.get_help()
                else:
                    response = f"Unknown subcommand: {subcommand}\n{self.get_help()}"
                    
            except Exception as e:
                self.logger.error(f"Error in repeater command: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
                response = f"Error executing repeater command: {e}"
        
        # Handle multi-message responses (like locations command)
        if isinstance(response, tuple) and response[0] == "multi_message":
            # Send first message
            await self.send_response(message, response[1])
            
            # Wait for bot TX rate limiter to allow next message
            import asyncio
            rate_limit = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
            # Use a conservative sleep time to avoid rate limiting
            sleep_time = max(rate_limit + 1.0, 2.0)  # At least 2 seconds, or rate_limit + 1 second
            await asyncio.sleep(sleep_time)
            
            # Send second message
            await self.send_response(message, response[2])
        else:
            # Send single message as usual
            await self.send_response(message, response)
        
        return True
    
    async def _handle_scan(self) -> str:
        """Scan contacts for repeaters (DEPRECATED - automatic in backend).

        Triggers a scan of the device's contact list to identify and catalog repeaters.

        Returns:
            str: Result message describing the scan outcome.
        """
        # Return deprecation warning
        return self._get_deprecation_warning() + "\nScanning happens automatically in the backend."
    
    async def _handle_list(self, args: List[str]) -> str:
        """List repeater contacts (DEPRECATED - use web viewer or prefix command).

        Args:
            args: Command arguments (ignored).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning() + "\nUse 'prefix <name>' to find specific repeaters or web viewer to browse all."
    
    async def _handle_purge(self, args: List[str]) -> str:
        """Purge repeater or companion contacts.
        
        Supports purging by name, age (days), or 'all'.
        
        Args:
            args: Command arguments specifying what to purge.
            
        Returns:
            str: Result message describing the purge outcome.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        if not args:
            return "Usage: !repeater purge [all|days|name|companions] [reason]\nExamples:\n  !repeater purge all 'Clear all repeaters'\n  !repeater purge companions 'Clear inactive companions'\n  !repeater purge companions 30 'Purge companions inactive 30+ days'\n  !repeater purge 30 'Auto-cleanup old repeaters'\n  !repeater purge 'Hillcrest' 'Remove specific repeater'"
        
        try:
            # Check if purging companions
            if args[0].lower() == 'companions':
                return await self._handle_purge_companions(args[1:])
            
            if args[0].lower() == 'all':
                # Check for force flag
                force_purge = len(args) > 1 and args[1].lower() == 'force'
                if force_purge:
                    reason = " ".join(args[2:]) if len(args) > 2 else "Force purge - all repeaters"
                else:
                    reason = " ".join(args[1:]) if len(args) > 1 else "Manual purge - all repeaters"
                
                # Always get repeaters directly from device contacts for purging
                # This ensures we have the correct contact_key for removal
                self.logger.info("Scanning device contacts for repeaters to purge...")
                device_repeaters = []
                if hasattr(self.bot.meshcore, 'contacts'):
                    for contact_key, contact_data in self.bot.meshcore.contacts.items():
                        if self.bot.repeater_manager._is_repeater_device(contact_data):
                            public_key = contact_data.get('public_key', contact_key)
                            name = contact_data.get('adv_name', contact_data.get('name', 'Unknown'))
                            device_repeaters.append({
                                'public_key': public_key,
                                'contact_key': contact_key,  # Include the contact key for removal
                                'name': name,
                                'contact_data': contact_data
                            })
                
                if not device_repeaters:
                    return "❌ No repeaters found on device to purge"
                
                repeaters = device_repeaters
                self.logger.info(f"Found {len(repeaters)} repeaters directly from device contacts")
                
                # Also catalog them in the database for future reference
                cataloged = await self.bot.repeater_manager.scan_and_catalog_repeaters()
                if cataloged > 0:
                    self.logger.info(f"Cataloged {cataloged} new repeaters in database")
                
                # Force a complete refresh of contacts from device after purging
                self.logger.info("Forcing contact list refresh from device to ensure persistence...")
                try:
                    await self.bot.meshcore.commands.get_contacts()
                    self.logger.info("Contact list refreshed from device")
                except Exception as e:
                    self.logger.warning(f"Failed to refresh contact list: {e}")
                
                purged_count = 0
                failed_count = 0
                failed_repeaters = []
                
                for i, repeater in enumerate(repeaters):
                    self.logger.info(f"Purging repeater {i+1}/{len(repeaters)}: {repeater['name']} (force={force_purge})")
                    
                    # Always use the new method that works with contact keys
                    success = await self.bot.repeater_manager.purge_repeater_by_contact_key(
                        repeater['contact_key'], reason
                    )
                    
                    if success:
                        purged_count += 1
                    else:
                        failed_count += 1
                        failed_repeaters.append(repeater['name'])
                    
                    # Add a small delay between purges to avoid overwhelming the device
                    if i < len(repeaters) - 1:
                        await asyncio.sleep(1)
                
                # Final verification: Check if contacts were actually removed from device
                self.logger.info("Performing final verification of contact removal...")
                try:
                    await self.bot.meshcore.commands.get_contacts()

                    # Count remaining repeaters on device
                    remaining_repeaters = sum(
                        1 for contact_data in self.bot.meshcore.contacts.values()
                        if self.bot.repeater_manager._is_repeater_device(contact_data)
                    )

                    self.logger.info(f"Final verification: {remaining_repeaters} repeaters still on device")

                except Exception as e:
                    self.logger.warning(f"Final verification failed: {e}")
                
                # Build response message
                purge_type = "Force purged" if force_purge else "Purged"
                response = f"✅ {purge_type} {purged_count}/{len(repeaters)} repeaters"
                if failed_count > 0:
                    response += f"\n❌ Failed to purge {failed_count} repeaters: {', '.join(failed_repeaters[:5])}"
                    if len(failed_repeaters) > 5:
                        response += f" (and {len(failed_repeaters) - 5} more)"
                    if not force_purge:
                        response += f"\n💡 Try '!repeater purge all force' to force remove stubborn repeaters"
                
                return response
                
            elif args[0].isdigit():
                # Purge old repeaters
                days = int(args[0])
                reason = " ".join(args[1:]) if len(args) > 1 else f"Auto-purge older than {days} days"
                
                purged_count = await self.bot.repeater_manager.purge_old_repeaters(days, reason)
                return f"✅ Purged {purged_count} repeaters older than {days} days"
            else:
                # Purge specific repeater by name (partial match)
                name_pattern = args[0]
                reason = " ".join(args[1:]) if len(args) > 1 else "Manual purge"
                
                # Find repeaters matching the name pattern
                repeaters = await self.bot.repeater_manager.get_repeater_contacts(active_only=True)
                matching_repeaters = [r for r in repeaters if name_pattern.lower() in r['name'].lower()]
                
                if not matching_repeaters:
                    return f"❌ No active repeaters found matching '{name_pattern}'"
                
                if len(matching_repeaters) == 1:
                    # Purge the single match
                    repeater = matching_repeaters[0]
                    success = await self.bot.repeater_manager.purge_repeater_from_contacts(
                        repeater['public_key'], reason
                    )
                    if success:
                        return f"✅ Purged repeater: {repeater['name']}"
                    else:
                        return f"❌ Failed to purge repeater: {repeater['name']}"
                else:
                    # Multiple matches - show options
                    lines = [f"Multiple repeaters found matching '{name_pattern}':"]
                    for i, repeater in enumerate(matching_repeaters, 1):
                        lines.append(f"{i}. {repeater['name']} ({repeater['device_type']})")
                    lines.append("")
                    lines.append("Please be more specific with the name.")
                    return "\n".join(lines)
                    
        except ValueError:
            return "❌ Invalid number of days. Please provide a valid integer."
        except Exception as e:
            return f"❌ Error purging repeaters: {e}"
    
    async def _handle_purge_companions(self, args: List[str]) -> str:
        """Purge companion contacts based on inactivity.
        
        Args:
            args: Command arguments (optional days threshold).
            
        Returns:
            str: Result message describing the purge outcome.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        if not self.bot.repeater_manager.companion_purge_enabled:
            return "❌ Companion purge disabled. Enable: [Companion_Purge] companion_purge_enabled = true"
        
        try:
            # Check for days argument
            days_old = None
            reason = "Manual purge - inactive companions"
            
            if args:
                try:
                    # Try to parse first arg as number of days
                    days_old = int(args[0])
                    reason = " ".join(args[1:]) if len(args) > 1 else f"Manual purge - companions inactive {days_old}+ days"
                except ValueError:
                    # Not a number, treat as reason
                    reason = " ".join(args) if args else "Manual purge - inactive companions"
            
            # Get companions for purging
            if days_old:
                # Purge companions inactive for specified days
                companions_to_purge = await self.bot.repeater_manager._get_companions_for_purging(999)  # Get all eligible
                # Filter by days
                from datetime import datetime, timedelta
                cutoff_date = datetime.now() - timedelta(days=days_old)
                filtered_companions = []
                for companion in companions_to_purge:
                    if companion.get('last_activity'):
                        try:
                            last_activity = datetime.fromisoformat(companion['last_activity'])
                            if last_activity < cutoff_date:
                                filtered_companions.append(companion)
                        except:
                            pass
                    elif companion.get('days_inactive', 0) >= days_old:
                        filtered_companions.append(companion)
                companions_to_purge = filtered_companions
            else:
                # Get companions based on configured thresholds
                companions_to_purge = await self.bot.repeater_manager._get_companions_for_purging(999)  # Get all eligible
            
            if not companions_to_purge:
                return "❌ No companions match criteria (inactive for DM+advert thresholds, not in ACL)"
            
            # Purge companions (compact format for 130 char limit)
            total_to_purge = len(companions_to_purge)
            purged_count = 0
            failed_count = 0
            
            for i, companion in enumerate(companions_to_purge):
                self.logger.info(f"Purging companion {i+1}/{total_to_purge}: {companion['name']}")
                
                success = await self.bot.repeater_manager.purge_companion_from_contacts(
                    companion['public_key'], reason
                )
                
                if success:
                    purged_count += 1
                else:
                    failed_count += 1
                
                # Add delay between purges to avoid overwhelming the radio
                # Use 2 seconds to give radio time to process each removal
                if i < total_to_purge - 1:
                    await asyncio.sleep(2)
            
            # Build compact response (must fit in 130 chars)
            if failed_count > 0:
                response = f"✅ {purged_count}/{total_to_purge} companions purged, {failed_count} failed"
            else:
                response = f"✅ {purged_count}/{total_to_purge} companions purged"
            
            # Truncate if still too long
            if len(response) > 130:
                response = f"✅ {purged_count}/{total_to_purge} purged"
            
            return response
            
        except Exception as e:
            return f"❌ Error purging companions: {e}"
    
    async def _handle_restore(self, args: List[str]) -> str:
        """Restore purged repeater contacts (DEPRECATED - use web viewer).

        Args:
            args: Command arguments (name pattern to restore).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning()
    
    async def _handle_stats(self) -> str:
        """Show statistics (DEPRECATED - use web viewer).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning() + "\nView detailed statistics in the web viewer."
    
    async def _handle_status(self) -> str:
        """Show contact list status and limits.
        
        Returns:
            str: Formatted status message showing usage vs limits.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        try:
            status = await self.bot.repeater_manager.get_contact_list_status()
            
            if not status:
                return "❌ Failed to get contact list status"
            
            # Shortened for LoRa transmission
            current = status['current_contacts']
            limit = status['estimated_limit']
            usage = status['usage_percentage']
            companions = status['companion_count']
            repeaters = status['repeater_count']
            stale = status['stale_contacts_count']
            
            if status['is_at_limit']:
                return f"📊 {current}/{limit} ({usage:.0f}%) | 👥{companions} 📡{repeaters} ⏰{stale} | 🚨 FULL!"
            elif status['is_near_limit']:
                return f"📊 {current}/{limit} ({usage:.0f}%) | 👥{companions} 📡{repeaters} ⏰{stale} | ⚠️ NEAR"
            else:
                return f"📊 {current}/{limit} ({usage:.0f}%) | 👥{companions} 📡{repeaters} ⏰{stale} | ✅ OK"
            
        except Exception as e:
            return f"❌ Error getting contact status: {e}"
    
    async def _handle_manage(self, args: List[str]) -> str:
        """Manage contact list (DEPRECATED - use web viewer).

        Args:
            args: Command arguments (e.g., '--dry-run').

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning()
    
    async def _handle_add(self, args: List[str]) -> str:
        """Add a discovered contact (DEPRECATED - use web viewer).

        Args:
            args: Command arguments (name, public_key, reason).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning()
    
    async def _handle_discover(self) -> str:
        """Discover companion contacts (DEPRECATED - automatic in backend).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning() + "\nDiscovery happens automatically in the backend."
    
    async def _handle_contact_stats(self) -> str:
        """Show statistics about the complete repeater tracking database.
        
        Returns:
            str: Formatted statistics summary.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        try:
            stats = await self.bot.repeater_manager.get_contact_statistics()
            
            response = "📊 **Contact Tracking Statistics:**\n\n"
            response += f"• **Total Contacts Ever Heard:** {stats.get('total_heard', 0)}\n"
            response += f"• **Currently Tracked by Device:** {stats.get('currently_tracked', 0)}\n"
            response += f"• **Recent Activity (24h):** {stats.get('recent_activity', 0)}\n\n"
            
            if stats.get('by_role'):
                response += "**By MeshCore Role:**\n"
                # Display roles in logical order
                role_order = ['repeater', 'roomserver', 'companion', 'sensor', 'gateway', 'bot']
                for role in role_order:
                    if role in stats['by_role']:
                        count = stats['by_role'][role]
                        role_display = role.title()
                        if role == 'roomserver':
                            role_display = 'RoomServer'
                        response += f"• {role_display}: {count}\n"
                
                # Show any other roles not in the standard list
                for role, count in stats['by_role'].items():
                    if role not in role_order:
                        response += f"• {role.title()}: {count}\n"
                response += "\n"
            
            if stats.get('by_type'):
                response += "**By Device Type:**\n"
                for device_type, count in stats['by_type'].items():
                    response += f"• {device_type}: {count}\n"
            
            return response
            
        except Exception as e:
            return f"❌ Error getting repeater statistics: {e}"
    
    async def _handle_auto_purge(self, args: List[str]) -> str:
        """Handle auto-purge commands.
        
        Args:
            args: Command arguments (trigger, enable, disable, monitor).
            
        Returns:
            str: Result message.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        try:
            if not args:
                # Show auto-purge status
                status = await self.bot.repeater_manager.get_auto_purge_status()
                # Shortened for LoRa transmission (130 char limit)
                current = status.get('current_count', 0)
                limit = status.get('contact_limit', 300)
                usage = status.get('usage_percentage', 0)
                enabled = status.get('enabled', False)
                
                if status.get('is_at_limit', False):
                    response = f"🔄 Auto-Purge: {'ON' if enabled else 'OFF'} | {current}/{limit} ({usage:.0f}%) | 🚨 FULL!"
                elif status.get('is_near_limit', False):
                    response = f"🔄 Auto-Purge: {'ON' if enabled else 'OFF'} | {current}/{limit} ({usage:.0f}%) | ⚠️ NEAR LIMIT"
                else:
                    response = f"🔄 Auto-Purge: {'ON' if enabled else 'OFF'} | {current}/{limit} ({usage:.0f}%) | ✅ OK"
                
                return response
            
            elif args[0].lower() == "trigger":
                # Manually trigger auto-purge
                success = await self.bot.repeater_manager.check_and_auto_purge()
                if success:
                    return "✅ Auto-purge triggered successfully"
                else:
                    return "ℹ️ Auto-purge check completed (no purging needed or failed)"
            
            elif args[0].lower() == "enable":
                # Enable auto-purge
                self.bot.repeater_manager.auto_purge_enabled = True
                return "✅ Auto-purge enabled"
            
            elif args[0].lower() == "disable":
                # Disable auto-purge
                self.bot.repeater_manager.auto_purge_enabled = False
                return "❌ Auto-purge disabled"
            
            elif args[0].lower() == "monitor":
                # Run periodic monitoring
                await self.bot.repeater_manager.periodic_contact_monitoring()
                return "📊 Periodic contact monitoring completed"
            
            else:
                return "❌ Unknown auto-purge command. Use: `!repeater auto-purge [trigger|enable|disable|monitor]`"
                
        except Exception as e:
            return f"❌ Error with auto-purge command: {e}"
    
    async def _handle_purge_status(self) -> str:
        """Show detailed purge status and recommendations.
        
        Returns:
            str: Formatted status message.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        try:
            status = await self.bot.repeater_manager.get_auto_purge_status()
            
            # Shortened for LoRa transmission
            current = status.get('current_count', 0)
            limit = status.get('contact_limit', 300)
            usage = status.get('usage_percentage', 0)
            threshold = status.get('threshold', 280)
            enabled = status.get('enabled', False)
            
            if status.get('is_at_limit', False):
                response = f"📊 Purge: {'ON' if enabled else 'OFF'} | {current}/{limit} ({usage:.0f}%) | 🚨 FULL! Run trigger now!"
            elif status.get('is_near_limit', False):
                response = f"📊 Purge: {'ON' if enabled else 'OFF'} | {current}/{limit} ({usage:.0f}%) | ⚠️ Near {threshold}"
            else:
                response = f"📊 Purge: {'ON' if enabled else 'OFF'} | {current}/{limit} ({usage:.0f}%) | ✅ Healthy"
            
            return response
            
        except Exception as e:
            return f"❌ Error getting purge status: {e}"
    
    async def _handle_test_purge(self) -> str:
        """Test the improved purge system.
        
        Runs a test purge operation without permanently removing valid contacts,
        useful for verifying system functionality.
        
        Returns:
            str: Test result message.
        """
        if not hasattr(self.bot, 'repeater_manager'):
            return "Repeater manager not initialized. Please check bot configuration."
        
        try:
            result = await self.bot.repeater_manager.test_purge_system()
            
            if result.get('success', False):
                # Shortened for LoRa transmission
                contact = result.get('test_contact', 'Unknown')
                initial = result.get('initial_count', 0)
                final = result.get('final_count', 0)
                removed = result.get('contacts_removed', 0)
                method = result.get('purge_method', 'Unknown')
                response = f"🧪 Test: {contact} | {initial}→{final} (-{removed}) | {method} | ✅ OK"
            else:
                # Shortened for LoRa transmission
                error = result.get('error', 'Unknown error')
                count = result.get('contact_count', 0)
                response = f"🧪 Test FAILED | {count} contacts | {error[:50]}..."
            
            return response
            
        except Exception as e:
            return f"❌ Error testing purge system: {e}"
    
    async def _handle_debug_purge(self) -> str:
        """Debug purge system (DEPRECATED - debug feature).

        Returns:
            str: Deprecation warning.
        """
        return "⚠️ DEPRECATED: Debug feature - check logs for purge system status."
    
    async def _handle_auto(self, args: List[str]) -> str:
        """Toggle auto settings (DEPRECATED - use config file).

        Returns:
            str: Deprecation warning.
        """
        return "⚠️ DEPRECATED: Edit config file to change auto-management settings."
    
    async def _handle_test(self, args: List[str]) -> str:
        """Test commands (DEPRECATED - debug feature).

        Returns:
            str: Deprecation warning.
        """
        return "⚠️ DEPRECATED: Debug feature - check logs for system status."
    
    async def _handle_locations(self) -> str:
        """Show location data (DEPRECATED - use web viewer map).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning() + "\nView locations on the interactive map in the web viewer."
    
    async def _handle_update_geolocation(self, dry_run: bool = False, batch_size: int = 10) -> str:
        """Update geolocation data (DEPRECATED - automatic in backend).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning() + "\nGeocoding happens automatically in the backend."
    
    def get_help(self) -> str:
        """Get help text for the repeater command (essential commands only)"""
        # Ultra-compact help for 150 char DM limit
        return "status|purge all|purge companions [days]|auto-purge\n⚠️ Use web viewer or 'prefix' cmd to browse"
    
    async def _handle_geocode(self, args: List[str]) -> str:
        """Handle geocoding (DEPRECATED - automatic in backend).

        Returns:
            str: Deprecation warning.
        """
        return self._get_deprecation_warning() + "\nGeocoding happens automatically in the backend."
    
    async def _get_geocoding_status(self) -> str:
        """Get geocoding status"""
        try:
            # Count contacts needing geocoding
            needing_geocoding = self.bot.repeater_manager.db_manager.execute_query('''
                SELECT COUNT(*) as count 
                FROM complete_contact_tracking 
                WHERE latitude IS NOT NULL 
                AND longitude IS NOT NULL 
                AND (city IS NULL OR city = '')
                AND last_geocoding_attempt IS NULL
            ''')
            
            # Count contacts with geocoding data
            with_geocoding = self.bot.repeater_manager.db_manager.execute_query('''
                SELECT COUNT(*) as count 
                FROM complete_contact_tracking 
                WHERE city IS NOT NULL AND city != ''
            ''')
            
            # Count total contacts with coordinates
            with_coords = self.bot.repeater_manager.db_manager.execute_query('''
                SELECT COUNT(*) as count 
                FROM complete_contact_tracking 
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ''')
            
            needing = needing_geocoding[0]['count'] if needing_geocoding else 0
            with_geo = with_geocoding[0]['count'] if with_geocoding else 0
            total_coords = with_coords[0]['count'] if with_coords else 0
            
            # Shortened for LoRa (130 char limit)
            if needing > 0:
                return f"🌍 Geocoding: {with_geo}/{total_coords} done, {needing} pending"
            else:
                return f"🌍 Geocoding: {with_geo}/{total_coords} complete ✅"
                
        except Exception as e:
            return f"❌ Geocoding status error: {e}"
