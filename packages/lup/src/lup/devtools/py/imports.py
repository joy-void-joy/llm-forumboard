"""Helpers for ``py imports`` — import graph exploration."""

import ast
import typing
from collections.abc import Iterator
from pathlib import Path

from lup.devtools.py.common import categorize_import

# ---------------------------------------------------------------------------
# py imports — import graph exploration
# ---------------------------------------------------------------------------


class ImportEntry(typing.TypedDict):
    module: str
    names: list[str]
    category: str


def collect_imports_from_source(source: str) -> list[ImportEntry]:
    """Parse source and extract import statements."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    def entries_of(node: ast.AST) -> Iterator[ImportEntry]:
        """Every import entry one node declares."""
        match node:
            case ast.Import(names=names):
                for alias in names:
                    yield ImportEntry(
                        module=alias.name,
                        names=[],
                        category=categorize_import(alias.name),
                    )
            case ast.ImportFrom(module=module, names=names) if module:
                yield ImportEntry(
                    module=module,
                    names=[alias.name for alias in names],
                    category=categorize_import(module),
                )

    return [entry for node in ast.walk(tree) for entry in entries_of(node)]


def format_import_entry(entry: ImportEntry) -> str:
    if entry["names"]:
        names = ", ".join(entry["names"])
        return f"from {entry['module']} import {names}"
    return f"import {entry['module']}"


def entry_matches_target(entry: ImportEntry, target: str) -> bool:
    module = entry["module"]
    if module == target or module.startswith(target + "."):
        return True
    if "." not in target:
        return False
    parent, _, leaf = target.rpartition(".")  # lup: ignore[string-split] — dotted path
    return leaf in entry["names"] and (
        module == parent or module.startswith(parent + ".")
    )


class ReverseImport(typing.TypedDict):
    """One project file importing the target, with its matching import line."""

    file: str
    import_line: str


def find_reverse_imports(target_module: str, project_root: Path) -> list[ReverseImport]:
    """Find project files that import the target module."""
    results: list[ReverseImport] = []  # lup: ignore[empty-collection] — scan fold
    search_dirs = [project_root / "src", project_root / "packages"]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for py_file in search_dir.rglob("*.py"):
            try:
                source = py_file.read_text()
            except OSError:
                continue

            for entry in collect_imports_from_source(source):
                if entry_matches_target(entry, target_module):
                    relative = py_file.relative_to(project_root)
                    results.append(
                        ReverseImport(
                            file=str(relative),
                            import_line=format_import_entry(entry),
                        )
                    )
                    break

    return sorted(results, key=lambda r: r["file"])
