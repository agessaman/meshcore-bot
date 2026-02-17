#!/usr/bin/env python3
"""
MeshCore Bot using the meshcore-cli and meshcore.py packages
Uses a modular structure for command creation and organization
"""

import argparse
import asyncio
import signal
import sys


def main():
    parser = argparse.ArgumentParser(
        description="MeshCore Bot - Mesh network bot for MeshCore devices"
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate config section names and exit before starting the bot (exit 1 on errors)",
    )

    args = parser.parse_args()

    if args.validate_config:
        from modules.config_validation import (
            SEVERITY_ERROR,
            SEVERITY_INFO,
            SEVERITY_WARNING,
            validate_config,
        )
        results = validate_config(args.config)
        has_error = False
        for severity, message in results:
            if severity == SEVERITY_ERROR:
                print(f"Error: {message}", file=sys.stderr)
                has_error = True
            elif severity == SEVERITY_WARNING:
                print(f"Warning: {message}", file=sys.stderr)
            else:
                print(f"Info: {message}", file=sys.stderr)
        sys.exit(1 if has_error else 0)

    from modules.core import MeshCoreBot
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
                
                # Handle bot task completion
                if bot_task:
                    if shutdown_event.is_set() and not bot_task.done():
                        # Shutdown triggered: cancel if still running
                        bot_task.cancel()
                    
                    # Always await bot_task to ensure proper cleanup
                    # This is necessary because:
                    # 1. If the task completed normally, we need to await to surface exceptions
                    # 2. If the task was cancelled, it only becomes "done" after being awaited
                    #    (cancellation is not immediate - the task must be awaited for the
                    #     CancelledError to be raised and the task to fully terminate)
                    try:
                        await bot_task
                    except asyncio.CancelledError:
                        # Expected when cancelled, ignore
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



