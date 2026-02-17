#!/usr/bin/env python3
"""
Validate MeshCore Bot config.ini section names.

Run standalone: python validate_config.py [--config config.ini]
Exits with 1 if any errors are found, 0 otherwise. Warnings and info are printed but do not affect exit code.
"""

import argparse
import sys

from modules.config_validation import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    validate_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate MeshCore Bot config.ini section names"
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )
    args = parser.parse_args()

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

    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
