"""base_service.py must import cleanly when loaded as local_services.base_service.

ServicePluginLoader supports local services that do
``from .base_service import BaseServicePlugin`` by executing this same file
under the ``local_services`` package, where relative parent imports
(``from ..x``) cannot resolve.
"""

import importlib.util
import sys
import types
from pathlib import Path


def test_base_service_imports_under_a_foreign_package_name():
    path = Path(__file__).resolve().parents[2] / "modules" / "service_plugins" / "base_service.py"
    pkg = types.ModuleType("local_services_probe")
    pkg.__path__ = []
    sys.modules["local_services_probe"] = pkg
    try:
        spec = importlib.util.spec_from_file_location("local_services_probe.base_service", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        assert hasattr(module, "BaseServicePlugin")
    finally:
        sys.modules.pop("local_services_probe.base_service", None)
        sys.modules.pop("local_services_probe", None)
