"""The type-resolution port the codescan grammar judges its sites against.

A grammar rule decides a site by what the symbol at one position *resolves
to* — `.get` reaching `dict` in typeshed is a mapping access, the same
spelling reaching `httpx.Client` is an HTTP request. Answering that needs a
type checker, which no library module may embed: `lup.codescan` stays
importable from the auditor with nothing but the standard library and
pydantic, and the hermetic hook kernel could not carry an inference engine at
all.

So the capability is a port. The audit depends on this ABC; the pyright
language-server client that implements it lives in devtools, is injected at
the call site, and is absent whenever the checker is not installed — in which
case the grammar resolves nothing and every rule keeps its broad regex
verdict. Positions are 1-based lines with 0-based UTF-8 column offsets, the
coordinates `ast` reports, so the library never speaks a protocol's encoding.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel


class SourcePosition(BaseModel, frozen=True):
    """One symbol's position in one Python file, in `ast` coordinates."""

    path: Path
    line: int
    """1-based line number, as `ast` reports `lineno`."""

    column: int
    """0-based UTF-8 column offset, as `ast` reports `col_offset`."""


class DefinitionSite(BaseModel, frozen=True):
    """Where a resolved symbol is declared, as the oracle reports it.

    The path may lead anywhere the checker looks — a project module, an
    installed package, a typeshed stub — and the grammar reads the declaring
    class out of it rather than trusting a rendered type name.
    """

    path: Path
    line: int
    """1-based line the declaration starts on."""


class DefinitionOracle(ABC):
    """Resolves the declarations of the symbols named at source positions."""

    @abstractmethod
    def definitions(
        self, positions: list[SourcePosition]
    ) -> list[list[DefinitionSite]]:
        """Each position's declarations, in order; empty where none resolve.

        Batched because resolution costs a checker session, not a lookup: one
        call answers a whole repository sweep. An unresolvable position yields
        an empty list rather than an error, which the grammar reads as "no
        evidence" and treats as a site it cannot refute.
        """
