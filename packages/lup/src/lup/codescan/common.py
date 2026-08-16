# lup: ignore[import-re, set-shape, empty-collection, dict-get, string-split] — the scanner that enforces these rules is written in the vocabulary they govern
"""Shared scanning core for the review-marker and anti-pattern scanners.

Both `lup.codescan.markers` and `lup.codescan.antipatterns` walk a file line by
line and must tell prose from code: a `#` that opens a real comment, or a
docstring, is where a review note or an `# lup: ignore` directive can live,
while the same characters inside an ordinary string literal are code.
Tokenizing and parsing the Python source answers that question once, here, so
neither scanner re-implements the other's mechanics.

The `# lup: ignore` escape hatch — inline, or as a standalone file-level
opt-out — is matched here too, `LineProjections` holds the token-masked line
views a context-aware rule scans, and `LineCursor` is the shared line walk that
lets a scanner absorb a note's continuation lines without index bookkeeping.

`PythonSource` is the unit whole-project scanners consume, and `Refutation`
the shape a refiner returns when it proves a matched line is not what its rule
is about — the one mechanism by which a broad regex hit is dropped with a
reason attached.
"""

import re
from collections.abc import Callable, Set as AbstractSet
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel

from lup.policy.kernel.edit import (
    FILE_IGNORE_RE,
    file_level_line,
    docstring_lines as python_docstring_lines,
    mask_python_string_literals,
    python_code_lines,
    python_comment_columns,
    python_tokens,
)

type RuleStrength = Literal["soft", "strong"]
"""Whether a rule admits a reasoned exception, or admits none.

Most rules are ``soft``: they name a shape that is usually wrong and sometimes
is the only thing that works — ``Any`` at an untyped boundary, a vendor's own
constant, a narrowing the type system needs. A suppression there is the
mechanism working, and the audit grades each one missing, bare, or dead.

A ``strong`` rule names a shape whose replacement is right every time. There is
no input for which an `isinstance` chain beats the `match` it compiles to, and
no call site where a bare tuple beats the `TypedDict` naming its fields — so a
suppression there is not a reasoned exception, it is the defect with a comment
on it. Those are refused, and the message says to write the replacement.
"""

type RuleContext = Literal["code", "comment"]
"""The syntactic surface a scan rule inspects: masked code, or comment text."""


class RuleSelection(BaseModel, frozen=True):
    """Which of the rules this library ships a project holds itself to.

    A rule is a convention written down and a convention is a judgement, so a
    repository that settled one differently is not defective there — it is
    answering a question this library had no standing to close. The tables
    reach a project as a starting point rather than as a fixture, named by the
    same ids a directive, a denial, and the generated reference already use.

    Subtractive, because a project disagreeing with three rules should say
    those three rather than restate the thirty it still keeps: a replacement
    table would have to be re-copied every time the library adds a rule, and
    the copy that fell behind would look like a decision.

    One selection reaches the sweep, the edit hook, and the generated
    reference together, so the three cannot disagree about which rules are
    live here.
    """

    retired: list[str] = []
    """Rule ids this project does not hold itself to."""

    def keeps(self, rule_id: str) -> bool:
        """Whether a rule is live here, for a scan deciding to run it."""
        return rule_id not in self.retired


# Both directive spellings come from the kernel rather than being restated
# here. The hook and this audit have to agree on what a directive *is* before
# they can agree on what it silences, and two regexes drifted apart is exactly
# how a file ends up exempt to one gate and denied by the other.


def ignore_rule_ids(match: re.Match[str]) -> set[str] | None:
    """Rule ids a matched `IGNORE_RE`/`FILE_IGNORE_RE` directive names.

    ``None`` is the bare, untyped `# lup: ignore` that silences every rule; a
    set names exactly the rules a typed `# lup: ignore[a, b]` silences (empty
    brackets yield an empty set — a typed directive that names nothing).
    """
    raw = match.group("ids")
    if raw is None:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


class FileIgnore(BaseModel):
    """A file-level `# lup: ignore` near a file's top and what it disables.

    ``rule_ids`` is ``None`` for a bare `# lup: ignore` that disables every
    rule for the whole file; a set names the rules a typed
    `# lup: ignore[rule-id]` disables file-wide. ``line`` is 1-based.
    """

    line: int
    rule_ids: set[str] | None


def file_level_ignore(text: str, max_lines: int = 10) -> FileIgnore | None:
    """The file-level `# lup: ignore` in the header block, or ``None``.

    A standalone bare `# lup: ignore` opts the whole file out of anti-pattern
    checks; the typed `# lup: ignore[rule-id]` form opts out only the named
    rules. Feedback-note scanning never consults this — an opted-out file still
    surfaces its `# lup:` notes (see `lup.codescan.markers.find_feedback`).
    """
    number = file_level_line(text, max_lines)
    if number == 0:
        return None
    match = FILE_IGNORE_RE.match(text.splitlines()[number - 1])
    return FileIgnore(
        line=number, rule_ids=ignore_rule_ids(match) if match is not None else None
    )


# lup: ignore[constant-declaration] — this library's own import root, fixed by the
# name it is distributed under rather than by any adopter's taste.
LIBRARY_PACKAGE_ROOT = "lup"

PACKAGE_ROOTS = frozenset({LIBRARY_PACKAGE_ROOT})  # lup: ignore[frozenset-shape]
"""Import roots a scan resolves module names against, by default this library's
own. An application adds the package it publishes, whose name it alone knows —
initialization renames it, so a value written down here would go on naming a
package that no longer exists and silently resolve nothing."""


class PythonSource(BaseModel, frozen=True):
    """One import-resolvable Python module a project-wide scanner reads.

    The unit every whole-project scan consumes: the architecture audit builds
    its symbol index from these, and the typed grammar parses them for the
    sites it judges.
    """

    path: Path
    module: str
    text: str


def module_name(path: Path, roots: AbstractSet[str] = PACKAGE_ROOTS) -> str:
    """Infer a dotted module name from a repository-relative Python path.

    A distribution laid out as ``packages/<name>/src/<name>/…`` repeats its
    name in the directory above ``src``, so the import root is the one ``src``
    introduces rather than the first segment that happens to match. Taking the
    first match instead resolves ``packages/lup/src/lup/harness/models.py`` to
    ``lup.src.lup.harness.models``, which no import ever names — every
    cross-module lookup against it silently misses.
    """
    parts = list(PurePosixPath(path.as_posix()).parts)
    matched = [
        index
        for index, part in enumerate(parts)
        if part in roots and (index == 0 or parts[index - 1] == "src")
    ]
    selected = parts[matched[-1] if matched else 0 :]
    if selected[-1] == "__init__.py":
        selected = selected[:-1]
    else:
        selected[-1] = PurePosixPath(selected[-1]).stem
    return ".".join(selected)


def sources_from_paths(
    paths: list[Path], roots: AbstractSet[str] = PACKAGE_ROOTS
) -> list[PythonSource]:
    """Read source files and assign import-resolvable module names."""
    return [
        PythonSource(
            path=path,
            module=module_name(path, roots),
            text=path.read_text(encoding="utf-8"),
        )
        for path in paths
    ]


class Refutation(BaseModel, frozen=True):
    """One rule hit a refiner proved does not apply, and the proof.

    A refiner sharpens a broad line rule after the fact: the regex says the
    shape is present, the refiner says this instance is not what the rule is
    about. The AST exemptions for deliberate empty-collection defaults and the
    typed grammar's receiver resolution both speak this shape, so the audit
    has one mechanism for "matched, but refuted" — and a `# lup: ignore` left
    guarding a refuted line becomes a dead directive the audit reports.

    ``subject`` is the source expression the verdict is about and ``evidence``
    the sentence that justifies it, so a dropped finding is always accountable.
    """

    rule_id: str
    line: int
    subject: str
    evidence: str


class PythonContext(BaseModel):
    """Where prose can live in a Python file: comment columns and docstrings.

    Built once per file by :meth:`parse`. ``comment_columns is None`` means the
    source did not tokenize; both queries then fall back to treating a position
    as prose so a note is never missed.
    """

    comment_columns: dict[int, int] | None
    docstring_lines: set[int]

    @classmethod
    def parse(cls, text: str) -> Self:
        return cls(
            comment_columns=python_comment_columns(text),
            docstring_lines=python_docstring_lines(text),
        )

    def comment_at(self, line_no: int, col: int) -> bool:
        """Whether a real `#` comment opens at (`line_no`, `col`)."""
        if self.comment_columns is None:
            return True
        return self.comment_columns.get(line_no) == col

    def is_note_context(self, line_no: int, col: int) -> bool:
        """Whether (`line_no`, `col`) sits in a comment or inside a docstring."""
        return self.comment_at(line_no, col) or line_no in self.docstring_lines


class LineProjections(BaseModel):
    """Per-context views of one file's lines for syntax-aware rule scanning.

    ``code`` blanks string-literal and comment tokens — the surface a
    "code"-context rule scans, so identifiers quoted in prose never trip it.
    ``commented`` blanks only string literals, keeping comments visible for
    "comment"-context directive rules. When the text does not tokenize as
    Python — a non-Python file or an incomplete fragment — both views fall
    back to the raw lines and ``tokenized`` is False, so a scanner can keep
    the conservative whole-line scan.
    """

    tokenized: bool
    code: list[str]
    commented: list[str]

    @classmethod
    def parse(cls, text: str) -> Self:
        return cls(
            tokenized=python_tokens(text) is not None,
            code=python_code_lines(text),
            commented=mask_python_string_literals(text),
        )

    def scan_text(self, line_no: int, context: RuleContext) -> str:
        """The stripped text a rule of `context` scans at 1-based `line_no`."""
        lines = self.code if context == "code" else self.commented
        return lines[line_no - 1].strip()


class NumberedLine(BaseModel):
    """One line of a scanned file, and its 1-based number."""

    number: int
    text: str


class MappedLine[T](BaseModel):
    """What a mapper made of one line, and the 1-based number it came from."""

    number: int
    value: T


class LineCursor:
    """Forward cursor over a file's lines with a take-a-run helper.

    Iterating yields ``(line_no, line)`` with a 1-based number.
    :meth:`take_mapping` pulls the run of following lines a mapper accepts,
    stopping before the first it rejects so iteration resumes there — the shape
    a marker note uses to absorb its continuation lines with no index
    bookkeeping in the caller.
    """

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.pos = 0

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> NumberedLine:
        if self.pos >= len(self.lines):
            raise StopIteration
        line = self.lines[self.pos]
        self.pos += 1
        return NumberedLine(number=self.pos, text=line)

    def take_mapping[T](
        self, mapper: Callable[[int, str], T | None]
    ) -> list[MappedLine[T]]:
        """Consume and map following lines until `mapper` returns ``None``.

        The rejecting line is left unconsumed for the next `__next__`. A mapper
        may return a falsy-but-not-``None`` value (an empty continuation line),
        which is kept; only ``None`` ends the run.
        """
        taken: list[MappedLine[T]] = []
        while self.pos < len(self.lines):
            line_no = self.pos + 1
            mapped = mapper(line_no, self.lines[self.pos])
            if mapped is None:
                break
            self.pos += 1
            taken.append(MappedLine(number=line_no, value=mapped))
        return taken
