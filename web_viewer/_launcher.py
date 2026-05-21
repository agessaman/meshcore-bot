#!/usr/bin/env python3
"""
Subprocess entry point for the web viewer.

Invoked via subprocess.Popen by WebViewerIntegration._run_viewer().
Runs in a fresh interpreter so it never shares state with the bot process.
"""
import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path when run as a subprocess script.
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from web_viewer.app import BotDataViewer


def _main() -> None:
    parser = argparse.ArgumentParser(description='MeshCore Bot Data Viewer')
    parser.add_argument('--config', default='config.ini')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    viewer = BotDataViewer(config_path=args.config)
    viewer.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    _main()
