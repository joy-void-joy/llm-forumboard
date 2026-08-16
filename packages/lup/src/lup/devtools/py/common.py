"""Helpers shared across ``py`` commands — dotted-path resolution, module paths, failure exit."""

import functools
import importlib
import importlib.util
import pkgutil
import sys
import typing
from pathlib import Path

import typer
from pydantic import BaseModel

from lup.workspace.paths import find_nearest_pyproject

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


class ResolvedObject(BaseModel):
    """A resolved dotted path: the live object and the leaf name that reached it."""

    value: object  # lup: ignore[bare-object] — any importable live object
    leaf_name: str


def resolve_object(path: str) -> ResolvedObject:
    """Resolve a dotted or colon path to a Python object, returning (object, leaf_name).

    Accepts both ``module.sub.Object`` (dot form) and the entry-point
    ``module.sub:Object.attr`` (colon form) — the two spellings
    ``pkgutil.resolve_name`` parses; it also walks the module-vs-attribute
    boundary itself, so no hand-rolled prefix-import loop is needed.
    """
    try:
        value = pkgutil.resolve_name(path)
    except (ImportError, AttributeError, ValueError) as e:
        raise ValueError(f"Could not resolve '{path}': {e}") from e
    module_part, _, attr_part = path.partition(":")  # lup: ignore[string-split]
    tail = attr_part or module_part
    # lup's resolve grammar: the leaf is the last dotted segment.
    leaf = tail.rpartition(".")[2]  # lup: ignore[string-split] — dotted-path leaf
    return ResolvedObject(value=value, leaf_name=leaf)


def find_module_path(module_name: str) -> Path | None:
    """Find the file path for a module."""
    try:
        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin:
            return Path(spec.origin)
    except (ImportError, ModuleNotFoundError, ValueError):
        pass
    try:
        mod = importlib.import_module(module_name)
        if hasattr(mod, "__file__") and mod.__file__:
            return Path(mod.__file__)
    except (ImportError, ModuleNotFoundError):
        pass
    return None


def fail(msg: str) -> typing.NoReturn:
    typer.echo(f"Error: {msg}", err=True)
    raise typer.Exit(1)


@functools.cache
def categorize_import(module_name: str) -> str:
    root = module_name.split(".")[0]  # lup: ignore[string-split] — dotted path
    if root in sys.stdlib_module_names:
        return "stdlib"
    path = find_module_path(root)
    if path is None:
        return "third-party"
    if "site-packages" in str(path):
        return "third-party"
    project_root = find_nearest_pyproject()
    if project_root and str(path).startswith(str(project_root)):
        return "project"
    return "third-party"
