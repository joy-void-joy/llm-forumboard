"""What a Python source defines, by qualified name and where.

A merge asks a question line-level diffing cannot answer: did this join lose
something one side had built? Line presence is the wrong instrument for it in
both directions — renaming a variable makes untouched lines read as missing,
while a deleted function whose body lines happen to recur elsewhere reads as
kept. A definition is the unit the question is actually about.

Names are qualified by the scope that holds them, so a method removed from
one class is not excused by a same-named method on another. The line comes
with the name because whoever is told a definition went missing has to find
it, and the tree it went missing from no longer holds it.
"""

import ast

from pydantic import BaseModel


class DefinedSymbol(BaseModel, frozen=True):
    """One name a source defines, and the line that defines it."""

    name: str
    """Qualified by the scopes that hold it, as ``Outer.inner``."""

    line: int


def symbols_under(node: ast.AST, prefix: str, local: bool) -> list[DefinedSymbol]:
    """Every definition beneath one node, qualified by the scope it sits in."""
    return [
        found
        for child in ast.iter_child_nodes(node)
        for found in symbols_of(child, prefix, local)
    ]


def symbols_of(node: ast.AST, prefix: str, local: bool) -> list[DefinedSymbol]:
    """One node's own definition, if it is one, and everything beneath it.

    ``local`` says the walk is inside a function body, where a bound name is
    a working variable rather than a definition. Recording those would put
    every renamed local on the lost list — the exact noise that makes a
    line-presence pass unreadable, arriving by a different route.
    """
    match node:
        case (
            ast.FunctionDef(name=name, lineno=line)
            | ast.AsyncFunctionDef(name=name, lineno=line)
        ):
            qualified = f"{prefix}{name}"
            return [
                DefinedSymbol(name=qualified, line=line),
                *symbols_under(node, f"{qualified}.", True),
            ]
        case ast.ClassDef(name=name, lineno=line):
            qualified = f"{prefix}{name}"
            return [
                DefinedSymbol(name=qualified, line=line),
                *symbols_under(node, f"{qualified}.", False),
            ]
        case (
            ast.Assign(targets=[ast.Name(id=name)], lineno=line)
            | ast.AnnAssign(target=ast.Name(id=name), lineno=line)
        ) if not local:
            return [DefinedSymbol(name=f"{prefix}{name}", line=line)]
    return symbols_under(node, prefix, local)


def defined_symbols(source: str) -> list[DefinedSymbol]:
    """Every function, class, and bound name this source defines.

    Unparseable text defines nothing rather than raising: this reads whatever
    a commit happens to hold, including a file that is not Python at all and
    a revision from before a syntax error was fixed.
    """
    try:
        return symbols_under(ast.parse(source), "", False)
    except (SyntaxError, ValueError):
        return []


def symbols_lost(before: str, after: str) -> list[DefinedSymbol]:
    """Definitions ``before`` holds that ``after`` no longer does."""
    held = {symbol.name for symbol in defined_symbols(after)}
    return [symbol for symbol in defined_symbols(before) if symbol.name not in held]
