#!/usr/bin/env python3
"""
MeshCore Bot using the meshcore-cli and meshcore.py packages
Uses a modular structure for command creation and organization
"""

import argparse
import asyncio
import configparser
import json
import signal
import sys

from modules.config_snapshot import config_to_redacted_sections, redacted_sections_to_ini_text


def _configure_unix_signal_handlers(loop, bot, shutdown_event: asyncio.Event) -> None:
    """Register Unix signal handlers for shutdown and config reload."""

    def shutdown_handler():
        """Signal handler for graceful shutdown."""
        print("\nShutting down...")
        # asyncio.add_signal_handler replaces SIGINT/SIGTERM handling for the loop; the
        # bot's threading.Event + connected flag must be set here too or the main loop
        # can run another iteration and restart the web viewer before stop() runs.
        bot._shutdown_event.set()
        bot.connected = False
        shutdown_event.set()

    def reload_handler():
        """Reload config on SIGHUP without exiting."""
        bot.logger.info("Received SIGHUP, reloading configuration...")
        success, msg = bot.reload_config()
        if success:
            bot.logger.info("SIGHUP config reload succeeded: %s", msg)
        else:
            bot.logger.warning("SIGHUP config reload failed: %s", msg)

    # Register shutdown signals
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    # Register config reload signal (Unix daemons convention)
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, reload_handler)


_MAX_CONFIG_ISSUES_SHOWN = 15


def _collect_config_issues(config_path: str) -> list[tuple[str, str]]:
    """Errors and warnings from the config linter; ``[]`` if the linter itself fails.

    Imported lazily and guarded so a broken linter can never block startup.
    """
    try:
        from modules.config_validation import (
            SEVERITY_ERROR,
            SEVERITY_WARNING,
            validate_config,
        )

        return [
            (sev, msg)
            for sev, msg in validate_config(config_path)
            if sev in (SEVERITY_ERROR, SEVERITY_WARNING)
        ]
    except Exception as exc:  # noqa: BLE001 - never block startup on the linter itself
        print(f"Config validation skipped: {exc}", file=sys.stderr)
        return []


def _report_config_issues(issues: list[tuple[str, str]], logger=None) -> None:
    """Report config-linter findings through *logger*, or stderr when there is none.

    A misspelled key is the most common cause of "why doesn't my setting work",
    and the linter usually names the intended key. Sending that only to stderr put
    it in the journal, where an operator reading the configured log file never saw
    it. Prefer the logger so the finding lands in the log; stderr remains the
    fallback for the window before the logger exists.
    """
    shown = issues[:_MAX_CONFIG_ISSUES_SHOWN]
    overflow = len(issues) - len(shown)

    def emit(severity: str, text: str) -> None:
        if logger is None:
            print(text, file=sys.stderr)
        elif severity == "error":
            logger.error(text)
        else:
            logger.warning(text)

    for severity, message in shown:
        emit(severity, f"Config {severity}: {message}")
    if overflow > 0:
        emit(
            "warning",
            f"Config: ... and {overflow} more issue(s); "
            "run with --validate-config for the full report",
        )


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
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print resolved config.ini with sensitive keys redacted and exit",
    )
    parser.add_argument(
        "--show-config-json",
        action="store_true",
        help="Print resolved config.ini as redacted JSON and exit",
    )

    args = parser.parse_args()

    if args.show_config and args.show_config_json:
        print("Error: --show-config and --show-config-json are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if args.show_config or args.show_config_json:
        cfg = configparser.ConfigParser()
        try:
            loaded_paths = cfg.read(args.config, encoding="utf-8")
        except configparser.Error as exc:
            print(f"Error: Invalid config file '{args.config}': {exc}", file=sys.stderr)
            sys.exit(1)

        if not loaded_paths:
            print(f"Error: Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)

        sections = config_to_redacted_sections(cfg)
        if args.show_config_json:
            print(json.dumps(sections, indent=2, sort_keys=True))
        else:
            print(redacted_sections_to_ini_text(sections))
        sys.exit(0)

    if args.validate_config:
        from modules.config_validation import (
            SEVERITY_ERROR,
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

    # Always sanity-check the config on normal startup too — misspelled
    # sections/keys otherwise fail silently and are the most common source of
    # "why doesn't my setting work" confusion. Non-fatal: warn and continue.
    # Collected here, before anything opens the database, but reported once the
    # bot's logger exists so the findings reach the configured log file.
    _config_issues = _collect_config_issues(args.config)

    # A restore requested by the viewer is applied only here, before importing
    # and constructing MeshCoreBot (which opens the DB and launches writers).
    # The service manager must restart the bot and viewer as one unit.
    from modules.database_restore import (
        DatabaseRestoreError,
        apply_pending_restores_from_config,
    )

    try:
        restore_results = apply_pending_restores_from_config(args.config)
    except DatabaseRestoreError as exc:
        print(f"Error: pending database restore was not applied: {exc}", file=sys.stderr)
        print(
            "The active database was left unchanged. Correct or remove the pending restore "
            "file before restarting.",
            file=sys.stderr,
        )
        sys.exit(1)
    for restore_result in restore_results:
        recovery = restore_result.recovery_backup_path
        recovery_note = f"; recovery backup: {recovery}" if recovery else ""
        print(f"Applied pending database restore: {restore_result.database_path}{recovery_note}")

    from modules.core import MeshCoreBot

    try:
        bot = MeshCoreBot(config_file=args.config)
    except Exception:
        # No logger to route them through, and a bad config is the likeliest reason
        # construction failed, so stderr is both the only channel left and the one
        # the operator is about to read.
        _report_config_issues(_config_issues)
        raise

    _report_config_issues(_config_issues, bot.logger)

    # Use asyncio.run() which handles KeyboardInterrupt properly
    # For SIGTERM, we'll handle it in the async context
    async def run_bot():
        """Run bot with proper signal handling"""
        loop = asyncio.get_running_loop()

        def meshcore_task_exception_handler(loop, context):
            """Log unhandled exceptions from asyncio tasks (e.g. meshcore reader)."""
            exc = context.get('exception')
            msg = context.get('message', 'Unhandled exception in task')
            if exc is not None:
                bot.logger.warning(
                    "%s: %s",
                    msg,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            else:
                bot.logger.warning("%s: %s", msg, context)

        loop.set_exception_handler(meshcore_task_exception_handler)

        # Set up signal handlers for graceful shutdown (Unix only)
        if sys.platform != 'win32':
            shutdown_event = asyncio.Event()
            bot_task = None

            try:
                # Register signal handlers
                _configure_unix_signal_handlers(loop, bot, shutdown_event)

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
                        # Ensure the bot loop sees shutdown even if the signal handler ordering
                        # left a race before cancel.
                        bot._shutdown_event.set()
                        bot.connected = False
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


