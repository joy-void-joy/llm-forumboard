"""Helpers for ``py search`` — find symbols across packages."""

import importlib
import importlib.metadata
import inspect
import typing

# ---------------------------------------------------------------------------
# py search — find symbols across packages
# ---------------------------------------------------------------------------


class SearchMatch(typing.TypedDict):
    symbol: str
    kind: str
    import_path: str


def scan_module_symbols(module_name: str, pattern: str) -> list[SearchMatch]:
    """Import a module and search dir() for matching symbols."""
    try:
        mod = importlib.import_module(module_name)
    except (ImportError, AttributeError, TypeError, RuntimeError, OSError):
        return []

    pattern_lower = pattern.lower()

    def match_of(name: str) -> SearchMatch | None:
        if name.startswith("_") or pattern_lower not in name.lower():
            return None
        member = getattr(mod, name, None)
        if member is None or inspect.ismodule(member):
            return None
        if inspect.isclass(member):
            kind = "class"
        elif inspect.isfunction(member) or inspect.isbuiltin(member):
            kind = "function"
        else:
            kind = type(member).__name__
        return SearchMatch(symbol=name, kind=kind, import_path=f"{module_name}.{name}")

    return [m for name in dir(mod) if (m := match_of(name)) is not None]


def get_top_level_packages() -> list[str]:
    """Get importable top-level package names from installed distributions."""
    return sorted(importlib.metadata.packages_distributions().keys())
