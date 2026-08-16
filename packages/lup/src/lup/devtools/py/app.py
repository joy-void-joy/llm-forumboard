"""Typer command tree for Python introspection: info, source, eval, imports, search."""

import ast
import inspect
from collections import defaultdict
from typing import Annotated

import typer

from lup.workspace.paths import find_nearest_pyproject
from lup.devtools.py.common import fail, find_module_path, resolve_object
from lup.devtools.py.imports import (
    ImportEntry,
    collect_imports_from_source,
    find_reverse_imports,
    format_import_entry,
)
from lup.devtools.py.info import (
    show_callable_info,
    show_class,
    show_module,
    show_value_info,
)
from lup.devtools.py.search import (
    get_top_level_packages,
    scan_module_symbols,
)
from lup.devtools.py.source import format_tree
from lup.devtools.subapps import subapp
from lup.sandbox.models import DockerUnreachableError

app = typer.Typer(no_args_is_help=True)
SUBAPP = subapp("py", "Python module introspection", app)


@app.command("info")
def info_cmd(
    path: Annotated[
        str,
        typer.Argument(
            help="Dotted path: module, module.Class, module.func, module.CONST"
        ),
    ],
    schema: Annotated[
        bool,
        typer.Option("--schema", help="Show JSON schema (Pydantic models)"),
    ] = False,
    private: Annotated[
        bool,
        typer.Option("--private", "-p", help="Include private members"),
    ] = False,
) -> None:
    """Inspect a Python object — adapts to modules, classes, functions, values."""
    try:
        resolved = resolve_object(path)
    except ValueError as e:
        fail(str(e))
    obj, name = resolved.value, resolved.leaf_name

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  {path}")
    typer.echo(f"{'=' * 60}")

    if inspect.ismodule(obj):
        show_module(obj, path, private)
    elif isinstance(obj, type):
        show_class(obj, schema, private)
    elif callable(obj):
        show_callable_info(obj, name)
    else:
        show_value_info(obj)

    typer.echo()


@app.command("source")
def source_cmd(
    path: Annotated[
        str,
        typer.Argument(help="Dotted path: module, module.Class, or module.func"),
    ],
    tree: Annotated[
        bool,
        typer.Option("--tree", "-t", help="Show package file tree instead of source"),
    ] = False,
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Number of lines to show (0 = all)"),
    ] = 50,
    start: Annotated[
        int,
        typer.Option(
            "--start",
            "-s",
            help="Starting line (file line for modules, offset within the object)",
        ),
    ] = 1,
) -> None:
    """View source code for a Python object, or a package file tree with --tree."""
    if tree:
        file_path = find_module_path(path)
        if file_path is None:
            fail(f"Could not find module '{path}'")
        if file_path.name == "__init__.py":
            package_root = file_path.parent
        else:
            typer.echo(str(file_path))
            return
        typer.echo(f"{package_root.name}/")
        for line in format_tree(package_root):
            typer.echo(line)
        return

    try:
        obj = resolve_object(path).value
    except ValueError as e:
        fail(str(e))

    if inspect.ismodule(obj):
        file_path = find_module_path(path)
        if file_path is None or not file_path.exists():
            fail(f"No source file for '{path}'")
        typer.echo(f"# {file_path}")
        source_lines = file_path.read_text().splitlines()
        start_idx = max(0, start - 1)
        if lines > 0:
            selected = source_lines[start_idx : start_idx + lines]
        else:
            selected = source_lines[start_idx:]
        typer.echo(f"# Lines {start_idx + 1}–{start_idx + len(selected)}")
        typer.echo()
        for i, line in enumerate(selected, start=start_idx + 1):
            typer.echo(f"{i:4d}  {line}")
        return

    if not callable(obj):
        fail(f"Cannot get source for '{path}': {type(obj).__name__} has no source")
    try:
        source = inspect.getsource(obj)
        source_file = inspect.getfile(obj)
        _, start_lineno = inspect.getsourcelines(obj)
        typer.echo(f"# {source_file}:{start_lineno}")
    except (TypeError, OSError) as e:
        fail(f"Cannot get source for '{path}': {e}")

    obj_lines = source.splitlines()
    start_idx = max(0, start - 1)
    if lines > 0:
        selected = obj_lines[start_idx : start_idx + lines]
    else:
        selected = obj_lines[start_idx:]
    typer.echo(f"# {len(obj_lines)} lines")
    if len(selected) < len(obj_lines):
        first = start_lineno + start_idx
        last = first + len(selected) - 1
        typer.echo(f"# Showing lines {first}–{last} (--lines 0 for all)")
    typer.echo()
    for i, line in enumerate(selected, start=start_lineno + start_idx):
        typer.echo(f"{i:4d}  {line}")


@app.command("eval")
def eval_cmd(
    expression: Annotated[str, typer.Argument(help="Python expression to evaluate")],
) -> None:
    """Evaluate a Python expression in the sandbox, with modules auto-imported.

    Only expressions are allowed (no statements). This project's source is
    mounted read-only, so `lup.*` and the application package import here
    exactly as they do on the host — but the expression runs in a container,
    so what it can reach is the container's, not this checkout's.
    """
    try:
        ast.parse(expression, mode="eval")
    except SyntaxError as e:
        fail(f"Invalid expression: {e}")

    try:
        from lup.devtools.py.sandbox_eval import evaluate_in_sandbox
    except ImportError:
        fail(
            "`py eval` runs in the sandbox, which needs the docker extra: "
            "`uv sync --extra docker`. For reading code rather than running "
            "it, `py info` and `py source` need nothing extra; for anything "
            "you will want twice, add a devtools command."
        )

    try:
        typer.echo(evaluate_in_sandbox(expression))
    except DockerUnreachableError as e:
        fail(
            f"{e}\n\nFor reading code rather than running it, `py info`, "
            "`py source`, and `py search` need no daemon at all."
        )


@app.command("imports")
def imports_cmd(
    module: Annotated[str, typer.Argument(help="Module to analyze")],
    reverse: Annotated[
        bool,
        typer.Option("--reverse", "-r", help="Find what imports this module"),
    ] = False,
    depth: Annotated[
        int,
        typer.Option("--depth", "-d", help="Transitive import depth"),
    ] = 1,
) -> None:
    """Show what a module imports, or what imports it (--reverse)."""
    if reverse:
        root = find_nearest_pyproject()
        if root is None:
            fail("Could not find project root (no pyproject.toml)")
        results = find_reverse_imports(module, root)
        if not results:
            typer.echo(f"No project files import '{module}'")
            return
        typer.echo(f"Files importing '{module}':\n")
        for hit in results:
            typer.echo(f"  {hit['file']}")
            typer.echo(f"    {hit['import_line']}")
        return

    file_path = find_module_path(module)
    if file_path is None:
        fail(f"Could not find module '{module}'")
    if not file_path.exists():
        fail(f"Module file does not exist: {file_path}")

    seen: set[str] = set()  # lup: ignore[set-shape, empty-collection] — visited
    current_modules = [module]

    for current_depth in range(1, depth + 1):
        next_modules: list[str] = []
        if current_depth > 1:
            typer.echo(f"\n--- Depth {current_depth} ---")

        all_entries: list[ImportEntry] = []
        for mod_name in current_modules:
            mod_path = find_module_path(mod_name)
            if mod_path is None or not mod_path.exists():
                continue
            try:
                source = mod_path.read_text()
            except OSError:
                continue
            all_entries.extend(collect_imports_from_source(source))

        grouped: defaultdict[str, list[ImportEntry]] = defaultdict(list)
        for entry in all_entries:
            if entry["module"] in seen:
                continue
            seen.add(entry["module"])
            grouped[entry["category"]].append(entry)
            next_modules.append(entry["module"])

        for category in ("project", "third-party", "stdlib"):
            entries = grouped[category]
            if not entries:
                continue
            typer.echo(f"\n{category} ({len(entries)}):")
            for entry in sorted(entries, key=lambda e: e["module"]):
                typer.echo(f"  {format_import_entry(entry)}")

        current_modules = next_modules


@app.command("search")
def search_cmd(
    pattern: Annotated[str, typer.Argument(help="Symbol name to search for")],
    package: Annotated[
        list[str] | None,
        typer.Option("--package", "-P", help="Limit to specific packages"),
    ] = None,
) -> None:
    """Search for symbols across installed packages by name (case-insensitive)."""
    if package:
        packages = list(package)
    else:
        packages = get_top_level_packages()

    if not package:
        typer.echo(f"Scanning {len(packages)} installed packages...", err=True)

    all_matches = [
        match for pkg in packages for match in scan_module_symbols(pkg, pattern)
    ]
    scanned = len(packages)

    if not all_matches:
        typer.echo(f"No matches for '{pattern}' in {scanned} packages")
        return

    typer.echo(f"Matches for '{pattern}':\n")
    for match in sorted(all_matches, key=lambda m: m["import_path"]):
        typer.echo(f"  {match['kind']:10s}  {match['import_path']}")

    typer.echo(f"\n({len(all_matches)} matches in {scanned} packages)")
