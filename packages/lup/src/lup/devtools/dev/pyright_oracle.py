"""Pyright's language server, implementing the codescan definition oracle.

`lup.codescan.grammar` judges a site by where its subject is declared, and
this is the only thing in the repository that can answer that. Pyright is
already the project's single type checker, but its CLI only reports
diagnostics — a declaration query needs the language-server protocol, so this
module speaks LSP to `pyright-langserver --stdio`: one server for a whole
sweep, every queried document opened once, one `textDocument/definition` per
site.

Nothing here decides anything about a rule. The client returns declaration
locations and the library reads the declaring class out of them, which keeps
the checker replaceable and the grammar testable against a fake. Every way
this can fail — the server is not installed, it will not start, it stops
answering — resolves to "no declarations", the same answer as a genuinely
unresolvable symbol, so the audit falls back to its broad regex verdict
rather than reporting a refutation it cannot support.
"""

import asyncio
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import typer
from pydantic import ValidationError

from lup.codeintel.client import lsp_session
from lup.codeintel.replies import LOCATIONS
from lup.codescan.oracle import DefinitionOracle, DefinitionSite, SourcePosition
from lup.types import JsonValue
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — the binary pyright installs under
SERVER_NAME = "pyright-langserver"

RESOLVE_TIMEOUT_SECONDS = 600.0
"""Budget for a whole sweep. Exhausting it degrades to the broad rule."""


def langserver_path() -> Path | None:
    """The language server shipped beside this interpreter, or on PATH."""
    beside = Path(sys.executable).parent / SERVER_NAME
    if beside.is_file():
        return beside
    located = shutil.which(SERVER_NAME)
    return Path(located) if located is not None else None


def locations_of(result: JsonValue) -> list[DefinitionSite]:
    """Decode one `textDocument/definition` result into declaration sites."""
    try:
        decoded = LOCATIONS.validate_python(result)
    except ValidationError:
        return []  # an unspecified result shape is no evidence, not an error
    if decoded is None:
        return []
    found = decoded if isinstance(decoded, list) else [decoded]
    return [
        DefinitionSite(
            path=Path(unquote(urlparse(location.uri).path)),
            line=location.range.start.line + 1,
        )
        for location in found
    ]


async def resolve(
    server: Path, root: Path, positions: list[SourcePosition]
) -> list[list[DefinitionSite]]:
    """Run one language-server session and answer every queried position."""
    async with lsp_session(server, root, name=SERVER_NAME) as session:
        return [
            locations_of(
                await session.request(
                    "textDocument/definition",
                    await session.position_in(
                        position.path, position.line, position.column
                    ),
                )
            )
            for position in positions
        ]


class PyrightOracle(DefinitionOracle):
    """Answers declaration queries by driving `pyright-langserver` over stdio."""

    def __init__(
        self, server: Path, root: Path, timeout: float = RESOLVE_TIMEOUT_SECONDS
    ) -> None:
        self.server = server
        self.root = root
        self.timeout = timeout

    def definitions(
        self, positions: list[SourcePosition]
    ) -> list[list[DefinitionSite]]:
        """Resolve a whole sweep in one server session, degrading on failure."""
        if not positions:
            return []
        try:
            return asyncio.run(
                asyncio.wait_for(
                    resolve(self.server, self.root, positions),
                    timeout=self.timeout,
                )
            )
        except (OSError, EOFError, TimeoutError, ValueError) as error:
            typer.echo(
                f"{SERVER_NAME}: {error} — anti-pattern findings keep their "
                "unrefined verdicts",
                err=True,
            )
            return [[] for _ in positions]


def default_oracle() -> DefinitionOracle | None:
    """The pyright-backed oracle, or None where the language server is absent."""
    server = langserver_path()
    return None if server is None else PyrightOracle(server, project_root())
