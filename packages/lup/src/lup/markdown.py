"""The cells a generated Markdown table is laid out from.

Prose written by a human is Markdown all the way down and needs nothing here.
What needs a renderer is a table *derived* from declarations: a pipe or a
newline in a value silently breaks the row it lands in, and the layout is the
same every time it is written by hand.

A cell holds the literal value it stands for and escapes that value as it
renders, so there is no way to put text in a table that does not survive being
there. What the kinds below differ on is the formatting the author meant
around the value — plain, code, a link — never whether the value inside stays
literal. The table itself is :class:`lup.harness.models.MarkdownTable`, a
prompt part like any other, which lays these out without looking inside them.
"""

import html
from abc import abstractmethod
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator


def contained(value: str) -> str:
    r"""Neutralize every character that would end a cell or a row early.

    The pipe is spelled ``\|`` rather than as an entity because GFM's table
    extension unescapes it while splitting the row, before anything parses
    what is inside a cell. So it survives in prose, inside a code span, and
    inside a raw HTML element alike, where ``&#124;`` survives only the two
    that decode an entity — inside a code span a reader is shown the entity
    itself. One spelling that reaches everywhere is what lets this be asked
    once rather than chosen per destination.

    Both line endings count: a lone carriage return ends a line for a Markdown
    reader exactly as a newline does, so leaving it would break the row one
    character short of the case anyone tests for.
    """
    return value.translate(str.maketrans({"|": r"\|", "\n": " ", "\r": " "}))


def escaped(value: str) -> str:
    """Generated text made safe to be the content of one cell.

    For a destination that decodes an escape. A Markdown code span does not:
    ``&lt;`` is shown there as written, while a bare ``<`` needs no help
    because nothing in a code span is read as markup — so a code span takes
    :func:`contained` alone and this would corrupt it.
    """
    return contained(html.escape(value))


class MarkdownCell(BaseModel, frozen=True):
    """One cell of a generated table, holding the value it displays.

    Every kind answers :meth:`render`, and every answer runs the value it
    holds through :func:`contained` — directly where the kind's destination
    reads a value verbatim, through :func:`escaped` where it decodes one — so
    a new kind of formatting is one class and cannot be the one that forgot.
    """

    # lup: solved: Add an anti-pattern on `model_config =` and instruct the agent to use
    # the modern `class A(BaseModel, frozen=True, ...)` notation instead, then
    # convert the ones we already have — this line is one of them.
    #
    # lup: solved: The human settled the scope: convert every site, library and
    # application alike, knowing the cost. Measured at the time: 305 in source
    # (301 library, 4 application) plus 35 in tests, of which ~201 are literal
    # `ConfigDict(...)` and ~97 are alias-bound `model_config = FROZEN`, where
    # `FROZEN = ConfigDict(frozen=True)` is declared once per module in
    # resolver/models.py, runtime/models.py and harness/models.py. Converting the
    # alias sites inlines `frozen=True` into ~97 class headers and removes those
    # shared declarations. That DRY reversal was chosen deliberately, against the
    # recommendation to exempt them — so do not treat an alias-bound site as
    # exempt, and delete the aliases rather than leaving them unreferenced.

    text: str

    @abstractmethod
    def render(self) -> str:
        """This cell's Markdown, with the value it holds escaped."""


class PlainCell(MarkdownCell, frozen=True):
    """A value shown as it reads."""

    type: Literal["plain"] = "plain"

    def render(self) -> str:
        return escaped(self.text)


class CodeCell(MarkdownCell, frozen=True):
    """A value marked as code by the fence Markdown spells with backticks."""

    type: Literal["code"] = "code"

    def render(self) -> str:
        return f"`{contained(self.text)}`"


class HtmlCodeCell(MarkdownCell, frozen=True):
    """A value marked as code by the HTML element rather than the fence.

    For a value that may itself hold a backtick — a rule's matching shape, a
    snippet quoting one — which no fence of a fixed length survives.
    """

    type: Literal["html_code"] = "html_code"

    def render(self) -> str:
        return f"<code>{escaped(self.text)}</code>"


class LinkCell(MarkdownCell, frozen=True):
    """A cell naming a page rather than describing one.

    The destination is held to the row's structure but not otherwise escaped:
    the characters Markdown reserves inside a destination are the ones a real
    path uses, so quoting them would break the link this cell exists to make.
    """

    type: Literal["link"] = "link"
    target: str

    def render(self) -> str:
        return f"[{escaped(self.text)}]({contained(self.target)})"


type TableCell = Annotated[
    PlainCell | CodeCell | HtmlCodeCell | LinkCell, Discriminator("type")
]
"""Any cell a generated table holds, parseable back from what it rendered."""
