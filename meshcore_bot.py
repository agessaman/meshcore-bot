#!/usr/bin/env python3
"""
MeshCore Bot using the meshcore-cli and meshcore.py packages
Uses a modular structure for command creation and organization
"""

import argparse
from ast import parse
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

    def signal_handler(sig, frame):
        print("\nShutting down...")
        asyncio.create_task(bot.stop())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}")
        asyncio.run(bot.stop())


if __name__ == "__main__":
    main()
