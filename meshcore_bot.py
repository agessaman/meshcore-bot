#!/usr/bin/env python3
"""
MeshCore Bot using the meshcore-cli and meshcore.py packages
Uses a modular structure for command creation and organization
"""

import argparse
import asyncio
import signal
import sys

# Import the modular bot
from modules.core import MeshCoreBot


def main():
    parser = argparse.ArgumentParser(
        description="MeshCore Bot - Mesh network bot for MeshCore devices"
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )

    args = parser.parse_args()

    bot = MeshCoreBot(config_file=args.config)
    
    # Use asyncio.run() which handles KeyboardInterrupt properly
    # For SIGTERM, we'll handle it in the async context
    async def run_bot():
        """Run bot with proper signal handling"""
        # Set up signal handlers for graceful shutdown (Unix only)
        if sys.platform != 'win32':
            loop = asyncio.get_running_loop()
            shutdown_event = asyncio.Event()
            bot_task = None
            
            def signal_handler():
                """Signal handler for graceful shutdown"""
                print("\nShutting down...")
                shutdown_event.set()
            
            try:
                # Register signal handlers
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, signal_handler)
                
                # Start bot
                bot_task = asyncio.create_task(bot.start())
                
                # Wait for shutdown or completion
                done, pending = await asyncio.wait(
                    [bot_task, asyncio.create_task(shutdown_event.wait())],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                # If shutdown triggered, cancel bot task
                if shutdown_event.is_set() and bot_task and not bot_task.done():
                    bot_task.cancel()
                    try:
                        await bot_task
                    except asyncio.CancelledError:
                        pass
            finally:
                # Always ensure cleanup happens
                await bot.stop()
        else:
            # Windows: just run and catch KeyboardInterrupt
            try:
                await bot.start()
            finally:
                await bot.stop()
    
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        # Cleanup already handled in run_bot's finally block
        print("\nShutdown complete.")
    except Exception as e:
        # Cleanup already handled in run_bot's finally block
        print(f"Error: {e}")


if __name__ == "__main__":
    main()



