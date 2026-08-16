"""Repoint every import of a module that moved between the two halves.

Deciding a utility belongs in the library rather than the application is a
one-line judgement and a hundred-line consequence: the module moves, and
every site that named it has to say the new name. Doing that by hand is
where the judgement gets abandoned, so the mechanical half is a command.

Tokens decide, text applies. ``tokenize`` reports every token with its exact
position, so a dotted module path in an import statement is located by
grammar rather than by pattern; the replacement is then spliced over that one
span, leaving the spacing, the parenthesized continuation, and the comment
beside it byte for byte untouched. Splicing a span rather than swapping token
for token is what lets a path change depth — a flat module moving under a
subpackage is the ordinary relocation, and three names do not fit where two
were. A module path appearing anywhere else — a log line, a docstring naming
the old home, a path in a comment — is not a module run in an import
statement and is never matched; :func:`surviving_mentions` reports those for
a human to read instead.

One form is deliberately left alone: ``from package import submodule`` is the
same tokens as importing a name from that package, and rewriting it would
mean guessing which. The repository's conventions ask for
``from module import symbol`` anyway, so the guess is not worth the reach —
such a site fails the type check with an unresolved import rather than
passing quietly, which is the outcome that gets it fixed.
"""

import ast
import io
import tokenize
from collections.abc import Collection, Iterator
from pathlib import Path

from pydantic import BaseModel

SOURCE_SUFFIXES = (".py", ".pyi")
"""Which files a sweep reads, for a caller that does not say.

The rewrite itself is Python's grammar and only ever will be, but what a
repository wants swept is a wider question than that: a tree carrying its
module paths in generated stubs or a sibling language passes the suffixes it
means rather than accepting these two.
"""


def dotted_parts(node: ast.expr) -> list[str] | None:
    """The names an attribute chain spells, outermost last."""
    match node:
        case ast.Name(id=name):
            return [name]
        case ast.Attribute(value=inner, attr=attribute):
            head = dotted_parts(inner)
            return None if head is None else [*head, attribute]
        case _:
            return None


def name_parts(dotted: str) -> list[str] | None:
    """The name tokens a dotted module path is made of, or None if it is not one.

    A module path is an expression in Python's own grammar — a name, or an
    attribute chain over one — so parsing it as that is both how the parts
    are read and how a typo is caught before it is used to rewrite a file.
    """
    try:
        return dotted_parts(ast.parse(dotted, mode="eval").body)
    except SyntaxError:
        return None


class Relocation(BaseModel, frozen=True):
    """One module path that moved, and where it moved to, as name tokens.

    Held as parts rather than as dotted text because that is what the rewrite
    splices: a run of ``NAME`` tokens replaces a run of ``NAME`` tokens, and
    the reading that turns a path into names happens once, at the boundary
    where somebody typed one.
    """

    old: list[str]
    new: list[str]


class ModuleRun(BaseModel, frozen=True):
    """The token span naming one module path, as indexes into the token list."""

    start: int
    end: int

    def renamed(
        self, tokens: list[tokenize.TokenInfo], moves: list[Relocation]
    ) -> list[str] | None:
        """The replacement name tokens for this run, if it moved.

        A move matches the module it names and every module beneath it, so
        relocating a package carries its submodules without each being
        declared. The result may be longer or shorter than what it replaces —
        a move into or out of a subpackage changes the path's depth.
        """
        named = [
            token.string
            for token in tokens[self.start : self.end + 1]
            if token.string != "."
        ]
        for move in moves:
            if named[: len(move.old)] == move.old:
                return [*move.new, *named[len(move.old) :]]
        return None


class ModuleEdit(BaseModel, frozen=True):
    """One module path to respell, as a span on one line of the source."""

    row: int
    start: int
    end: int
    text: str


class RelocationEdit(BaseModel, frozen=True):
    """One file the rewrite changed, and how many imports it repointed."""

    path: Path
    imports: int


def dotted_run(tokens: list[tokenize.TokenInfo], start: int) -> int:
    """The index of the last token in the ``a.b.c`` run beginning at ``start``."""
    position = start
    while position + 2 < len(tokens):
        following = tokens[position + 1]
        if following.type != tokenize.OP or following.string != ".":
            break
        if tokens[position + 2].type != tokenize.NAME:
            break
        position += 2
    return position


def module_runs(tokens: list[tokenize.TokenInfo]) -> list[ModuleRun]:
    """Every token span naming a module in an import statement.

    A module path is named in exactly two places: after ``from``, and after
    ``import`` where no ``from`` preceded it on that logical line — including
    after each comma in that form, because ``import a.b, c.d`` names two.
    Names after an ``import`` that follows a ``from`` are the imported
    symbols, which are not module paths and must not be rewritten.
    """

    def run_at(index: int) -> Iterator[ModuleRun]:
        """The module run starting just past ``index``, if a name is there."""
        if index + 1 < len(tokens) and tokens[index + 1].type == tokenize.NAME:
            yield ModuleRun(start=index + 1, end=dotted_run(tokens, index + 1))

    def found() -> Iterator[ModuleRun]:
        from_seen = False
        listing = False
        for index, token in enumerate(tokens):
            if token.type in (tokenize.NEWLINE, tokenize.NL):
                from_seen = listing = False
            elif listing and token.type == tokenize.OP and token.string == ",":
                yield from run_at(index)
            elif token.type != tokenize.NAME:
                continue
            elif token.string == "from":
                from_seen = True
                yield from run_at(index)
            elif token.string == "import" and not from_seen:
                listing = True
                yield from run_at(index)

    return list(found())


def module_edits(
    tokens: list[tokenize.TokenInfo], moves: list[Relocation]
) -> list[ModuleEdit]:
    """Every module path in an import statement that one of the moves renames.

    A run continued across lines is declined rather than half-applied: the
    splice is a span on one line, and a path broken over two is rare enough
    that reporting it as a surviving mention beats guessing at the join.
    """

    def found() -> Iterator[ModuleEdit]:
        for run in module_runs(tokens):
            renamed = run.renamed(tokens, moves)
            start, end = tokens[run.start].start, tokens[run.end].end
            if renamed is None or start[0] != end[0]:
                continue
            yield ModuleEdit(
                row=start[0], start=start[1], end=end[1], text=".".join(renamed)
            )

    return list(found())


def apply_edits(text: str, edits: list[ModuleEdit]) -> str:
    """Splice every respelled module path into the source that named it.

    Rightmost first, so an edit's recorded columns still address the line it
    was read from when two imports share one.
    """
    lines = text.splitlines(keepends=True)
    for edit in sorted(edits, key=lambda edit: edit.start, reverse=True):
        line = lines[edit.row - 1]
        lines[edit.row - 1] = f"{line[: edit.start]}{edit.text}{line[edit.end :]}"
    return "".join(lines)


def relocate_in_file(path: Path, moves: list[Relocation]) -> RelocationEdit | None:
    """Repoint every import in one file, or report that none named a mover."""
    text = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    edits = module_edits(tokens, moves)
    if not edits:
        return None
    path.write_text(apply_edits(text, edits), encoding="utf-8")
    return RelocationEdit(path=path, imports=len(edits))


def source_files(
    roots: list[Path], suffixes: Collection[str] = SOURCE_SUFFIXES
) -> list[Path]:
    """Every source file beneath the given roots, in a stable order."""
    return sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.suffix in suffixes and "__pycache__" not in path.parts
    )


def relocate(
    roots: list[Path],
    moves: list[Relocation],
    suffixes: Collection[str] = SOURCE_SUFFIXES,
) -> list[RelocationEdit]:
    """Repoint every import beneath ``roots``, reporting each file changed."""
    edits = [relocate_in_file(path, moves) for path in source_files(roots, suffixes)]
    return [edit for edit in edits if edit is not None]


def surviving_mentions(
    roots: list[Path],
    moves: list[Relocation],
    suffixes: Collection[str] = SOURCE_SUFFIXES,
) -> list[str]:
    """Every remaining mention of a moved module, wherever it is not an import.

    Not necessarily wrong — prose about where something used to live is a
    legitimate thing to write — so this reports and the reader decides.
    """
    return [
        f"{path}:{number}: {line.strip()}"
        for path in source_files(roots, suffixes)
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if any(".".join(move.old) in line for move in moves)
    ]
