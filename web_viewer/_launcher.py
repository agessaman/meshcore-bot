#!/usr/bin/env python3
"""
Eventlet entry point for the web viewer subprocess.

eventlet.monkey_patch() MUST run before any other import so that
eventlet can replace stdlib sockets/threads cleanly.  This module
exists solely to enforce that ordering — it is invoked via
subprocess.Popen, never imported by the bot process.
"""
import gevent.monkey                 # noqa: E402 — must be first
gevent.monkey.patch_all()           # noqa: E402 — must be before all other imports

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
