"""Stop the web viewer when the bot process that launched it is gone.

systemd and Docker reap an orphaned viewer. A manual run does not, so a
SIGKILL of the bot leaves the viewer holding its port. The bot passes its
pid as ``--exit-with-parent``. This module polls that pid and shuts the
viewer down once the bot is gone.

On POSIX the kernel reparents an orphan, so the parent is gone when
``os.getppid()`` no longer matches the pid we were given. ``os.kill(pid, 0)``
is not enough: the pid can be reused by an unrelated process.

Windows does not change ``os.getppid()`` when the parent exits. The watcher
opens a process handle at start and waits on that handle, so a reused pid
cannot look like the original parent. If the handle cannot be opened
(access denied), the watcher does nothing: shutting down would be a guess.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable

# How often to recheck, and how long to wait after SIGTERM before forcing exit.
# SIGTERM lets the viewer's existing handler unwind; os._exit covers a handler
# that returns and leaves the port bound.
POLL_INTERVAL_SEC = 2.0
HARD_EXIT_GRACE_SEC = 5.0

PARENT_EXITED_MESSAGE = "Parent process exited; shutting down web viewer"

_VIEWER_LOGGER_NAME = "modern_web_viewer"

# Win32 wait. Only consulted when sys.platform == "win32".
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

_kernel32_dll = None


def parent_is_gone(parent_pid: int) -> bool:
    """Return whether ``parent_pid`` is no longer this process's live parent.

    POSIX compares ``os.getppid()`` to ``parent_pid``. Windows opens a handle
    for this single call, which can race with pid reuse; :func:`watch_parent`
    opens its handle once and reuses it.
    """
    if sys.platform == "win32":
        handle, already_gone, unwatchable = _open_windows_parent(parent_pid)
        try:
            if unwatchable:
                return False
            if already_gone:
                return True
            if handle is None:
                return False
            return _windows_process_exited(handle)
        finally:
            _close_windows_handle(handle)
    return os.getppid() != parent_pid


def watch_parent(
    parent_pid: int,
    *,
    logger: logging.Logger | None = None,
    poll_interval: float = POLL_INTERVAL_SEC,
    hard_exit_grace: float = HARD_EXIT_GRACE_SEC,
    stop_event: threading.Event | None = None,
    on_parent_gone: Callable[[], None] | None = None,
) -> None:
    """Poll until ``parent_pid`` is gone, then shut this process down.

    ``on_parent_gone`` replaces the shutdown. Tests use it so observing the
    decision does not kill the test runner. ``stop_event`` ends the wait
    without shutting down.
    """
    handle: int | None = None
    already_gone = False
    try:
        if sys.platform == "win32":
            handle, already_gone, unwatchable = _open_windows_parent(parent_pid)
            if unwatchable:
                # No reliable wait on this process. Staying up matches a
                # standalone viewer rather than exiting on a guess.
                _logger(logger).warning(
                    "Cannot watch parent process %s on Windows; the viewer will keep running",
                    parent_pid,
                )
                return
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if already_gone or _parent_is_gone(parent_pid, handle):
                break
            if _wait(stop_event, poll_interval):
                return
        closing, handle = handle, None
        _close_windows_handle(closing)
        if on_parent_gone is not None:
            on_parent_gone()
            return
        _shutdown_because_parent_exited(logger, hard_exit_grace)
    finally:
        _close_windows_handle(handle)


def start_parent_watcher(
    parent_pid: int,
    *,
    logger: logging.Logger | None = None,
) -> threading.Thread:
    """Start the daemon thread that exits this process when ``parent_pid`` dies."""
    thread = threading.Thread(
        target=watch_parent,
        kwargs={"parent_pid": parent_pid, "logger": logger},
        name="web-viewer-parent-watch",
        daemon=True,
    )
    thread.start()
    return thread


def _parent_is_gone(parent_pid: int, handle: int | None) -> bool:
    if sys.platform == "win32":
        if handle is None:
            return False
        return _windows_process_exited(handle)
    # Reparented when the launcher dies. A recycled pid would still pass
    # kill(pid, 0), so only the ppid comparison counts.
    return os.getppid() != parent_pid


def _wait(stop_event: threading.Event | None, poll_interval: float) -> bool:
    """Sleep for one poll. Return True when ``stop_event`` is set."""
    if stop_event is None:
        time.sleep(poll_interval)
        return False
    return stop_event.wait(poll_interval)


def _shutdown_because_parent_exited(logger: logging.Logger | None, grace: float) -> None:
    log = _logger(logger)
    log.info(PARENT_EXITED_MESSAGE)
    _flush_logger(log)
    try:
        _request_sigterm()
    except Exception:
        log.debug("SIGTERM to self failed", exc_info=True)
    # The signal handler may ignore SIGTERM or take a moment to unwind.
    # Do not leave the viewer holding the port.
    time.sleep(grace)
    os._exit(0)


def _request_sigterm() -> None:
    if sys.platform == "win32":
        # os.kill(SIGTERM) is TerminateProcess and skips Python handlers.
        signal.raise_signal(signal.SIGTERM)
        return
    os.kill(os.getpid(), signal.SIGTERM)


def _logger(logger: logging.Logger | None) -> logging.Logger:
    if logger is not None:
        return logger
    return logging.getLogger(_VIEWER_LOGGER_NAME)


def _flush_logger(logger: logging.Logger) -> None:
    current: logging.Logger | None = logger
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for handler in list(current.handlers):
            try:
                handler.flush()
            except Exception:
                continue
        if not current.propagate:
            break
        current = current.parent


def _kernel32():
    global _kernel32_dll
    if _kernel32_dll is not None:
        return _kernel32_dll
    from ctypes import wintypes

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    dll.GetExitCodeProcess.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = (wintypes.HANDLE,)
    dll.CloseHandle.restype = wintypes.BOOL
    _kernel32_dll = dll
    return dll


def _open_windows_parent(parent_pid: int) -> tuple[int | None, bool, bool]:
    """Return ``(handle, already_gone, unwatchable)``.

    The handle is the process object, not a later lookup by pid.
    """
    if parent_pid <= 0:
        return None, True, False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
        False,
        parent_pid,
    )
    if not handle:
        if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
            return None, False, True
        return None, True, False
    return int(handle), False, False


def _windows_process_exited(handle: int) -> bool:
    from ctypes import wintypes

    kernel32 = _kernel32()
    waited = kernel32.WaitForSingleObject(handle, 0)
    if waited == _WAIT_OBJECT_0:
        return True
    if waited == _WAIT_TIMEOUT:
        return False
    # Wait failed. The exit code is the backup; STILL_ACTIVE means running.
    code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        return False
    return int(code.value) != _STILL_ACTIVE


def _close_windows_handle(handle: int | None) -> None:
    if sys.platform != "win32" or not handle:
        return
    _kernel32().CloseHandle(handle)
