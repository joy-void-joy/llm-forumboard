"""Helpers for ``py eval`` — expression evaluation inside the sandbox.

Named ``evaluate`` rather than ``eval`` to avoid shadowing the builtin.

There is no denylist here. One stood between the expression and the
interpreter for a while, refusing ``os``, ``open``, ``getattr`` and a few
dozen more — and it was escapable three ways in one line
(``importlib.import_module("os")``, ``zipfile.os``,
``importlib.import_module("pathlib").Path(...).read_text()``), so it stopped
nothing an agent could not already reach through the tools it is handed
anyway. What it did do was refuse ordinary introspection with a message that
read like a security verdict. The isolation that was being approximated is
the container's, and that is where the expression now runs.
"""

import ast
import importlib
import json

from lup.types import Namespace
from pprint import pformat

DEFAULT_BUILTINS: Namespace = {
    "True": True,
    "False": False,
    "None": None,
    "dir": dir,
    "type": type,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "len": len,
    "callable": callable,
    "id": id,
    "hash": hash,
    "repr": repr,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "bytes": bytes,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,  # lup: ignore[set-shape] — the builtin itself
    "frozenset": frozenset,  # lup: ignore[frozenset-shape] — the builtin
    "sorted": sorted,
    "reversed": reversed,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "range": range,
    "min": min,
    "max": max,
    "sum": sum,
    "any": any,
    "all": all,
    "print": print,
    "iter": iter,
    "next": next,
    "abs": abs,
    "round": round,
    "ord": ord,
    "chr": chr,
    "hex": hex,
    "bin": bin,
    "oct": oct,
}


def auto_import_namespace(
    tree: ast.Expression,
    builtins: Namespace = DEFAULT_BUILTINS,
) -> Namespace:
    """Build a namespace by importing modules referenced in the expression."""
    namespace = dict(builtins)

    walked = list(ast.walk(tree.body))
    root_names = [
        node.id
        for node in walked
        if isinstance(node, ast.Name) and node.id not in namespace
    ]
    attr_nodes = [node for node in walked if isinstance(node, ast.Attribute)]

    for name in dict.fromkeys(root_names):
        try:
            namespace[name] = importlib.import_module(name)
        except ImportError:
            pass

    dotted_paths: list[str] = []  # lup: ignore[empty-collection] — chain fold
    for node in attr_nodes:
        chain: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            chain.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and current.id in namespace:
            chain.append(current.id)
            chain.reverse()
            for i in range(2, len(chain) + 1):
                dotted_paths.append(".".join(chain[:i]))

    for dotted in sorted(dict.fromkeys(dotted_paths)):
        try:
            importlib.import_module(dotted)
        except ImportError:
            pass

    return namespace


def sandbox_program(expression: str) -> str:
    """The cell that evaluates one expression inside the container.

    It reaches the same auto-import and formatting the command has always
    used, through the read-only source mount rather than by reimplementing
    them, so an expression cannot mean one thing here and another there. The
    expression travels as a literal because a REPL cell has no argv.
    """
    return (
        "import ast\n"
        "from lup.devtools.py.evaluate import ("
        "auto_import_namespace, format_eval_result)\n"
        f"tree = ast.parse({expression!r}, mode='eval')\n"
        "print(format_eval_result("
        "eval(compile(tree, '<py eval>', 'eval'), auto_import_namespace(tree))))\n"
    )


def format_eval_result(result: object) -> str:  # lup: ignore[bare-object] — eval result
    containers = (dict, list, tuple, set, frozenset)  # lup: ignore[frozenset-shape]
    if isinstance(result, containers):
        try:
            return json.dumps(result, indent=2, default=repr)
        except (TypeError, ValueError):
            return pformat(result, width=100)
    return pformat(result, width=100)
