"""Factory for selecting the configured radio backend."""

from __future__ import annotations

from typing import Any


def create_radio_backend(config: Any, db_manager: Any, logger: Any, *, radio_debug: bool = False) -> Any:
    """Create the configured radio backend.

    Imports are intentionally lazy so deployments only need the selected backend's
    dependencies installed.
    """

    connection_type = config.get("Connection", "connection_type", fallback="ble").lower()
    if connection_type == "pymc":
        from .pymc_core_backend import PyMcCoreBackend

        return PyMcCoreBackend(config, db_manager, logger)

    from .meshcore_py_backend import MeshcorePyBackend

    return MeshcorePyBackend(
        config,
        logger,
        connection_type,
        radio_debug=radio_debug,
    )
