"""One strategy per module; ``load("name")`` returns the Strategy subclass defined in it."""
import importlib
import inspect
import re

from .base import Strategy


def load(name: str) -> type[Strategy]:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name == "base":
        raise ValueError(f"Invalid strategy name: {name!r}")
    module = importlib.import_module(f"{__name__}.{name}")
    found = [obj for obj in vars(module).values()
             if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__]
    if len(found) != 1:
        raise ValueError(f"Module {name} must define exactly one Strategy subclass")
    return found[0]
