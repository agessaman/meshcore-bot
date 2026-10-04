"""Every bundled command takes ``get_help_text(message=None)``.

help and command_manager still retry without the message when a command's
get_help_text takes none (local plugins may be written that way), but bundled
commands use the one signature.
"""

import importlib
import inspect
import pkgutil

import modules.commands
import modules.commands.alternatives
from modules.commands.base_command import BaseCommand


def _bundled_command_classes():
    for package in (modules.commands, modules.commands.alternatives):
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{package.__name__}.{info.name}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, BaseCommand) and cls.__module__ == module.__name__:
                    yield cls


def test_every_bundled_get_help_text_takes_an_optional_message():
    classes = list(_bundled_command_classes())
    assert len(classes) > 40
    wrong = []
    for cls in classes:
        params = inspect.signature(cls.get_help_text).parameters
        message = params.get("message")
        if message is None or message.default is not None:
            wrong.append(cls.__name__)
    assert wrong == []
