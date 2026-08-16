"""One language-server session over stdio, answering arbitrary requests.

A language server resolves names the way the language does — through imports,
aliases, and re-exports — which is the difference between finding a symbol and
finding characters that look like one. The protocol is the same for every
question worth asking it, so the framing, the request/response pairing, and
the document bookkeeping are written once here and each caller supplies the
method it wants.

Nothing here decides which server to run: the executable and the workspace
root are supplied, so a project that type-checks with something else answers
these questions with something else. Failures are the caller's to interpret —
a sweep that can degrade to a weaker verdict and a tool that must report the
truth to an agent want opposite things from a server that will not start.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from email.parser import BytesParser
from pathlib import Path

from lup.types import JsonObject, JsonValue


def utf16_column(line: str, column: int) -> int:
    """Convert a UTF-8 byte offset into the UTF-16 offset LSP positions use."""
    prefix = line.encode("utf-8")[:column].decode("utf-8", errors="ignore")
    return len(prefix.encode("utf-16-le")) // 2


class LspSession:
    """An initialized language server, and the documents opened against it."""

    def __init__(
        self,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        name: str,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.name = name
        self.asked = 0
        self.opened: dict[str, list[str]] = {}

    async def notify(self, method: str, params: JsonObject) -> None:
        """Send a notification, which the protocol never answers."""
        await self.frame({"jsonrpc": "2.0", "method": method, "params": params})

    async def frame(self, body: JsonObject) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        await self.stdin.drain()

    async def receive(self) -> JsonObject:
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            chunk = await self.stdout.readline()
            if not chunk:
                raise OSError(f"{self.name} closed its output stream")
            header.extend(chunk)
        declared = BytesParser().parsebytes(bytes(header))["Content-Length"]
        if declared is None:
            raise OSError(f"{self.name} sent a frame with no Content-Length")
        message: JsonObject = json.loads(await self.stdout.readexactly(int(declared)))
        return message

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        """Ask one question and return its answer, skipping unrelated traffic.

        A server interleaves diagnostics and progress with the replies it
        owes, so the reply is matched by id rather than by arrival order.
        """
        self.asked += 1
        asked = self.asked
        await self.frame(
            {"jsonrpc": "2.0", "method": method, "params": params, "id": asked}
        )
        while True:
            message = await self.receive()
            if "id" in message and message["id"] == asked:
                return message["result"] if "result" in message else None

    async def open(self, path: Path) -> list[str]:
        """Open a document once per session and return its lines.

        The lines come back because an LSP position is a UTF-16 offset into
        one of them, so a caller holding a byte offset needs the text that
        offset is into.
        """
        key = path.as_posix()
        if key in self.opened:
            return self.opened[key]
        text = path.read_text(encoding="utf-8")
        self.opened[key] = text.splitlines()
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.absolute().as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )
        return self.opened[key]

    async def position_in(self, path: Path, line: int, column: int) -> JsonObject:
        """The `TextDocumentPositionParams` for a one-based line and byte column."""
        lines = await self.open(path)
        row = lines[line - 1] if line <= len(lines) else ""
        return {
            "textDocument": {"uri": path.absolute().as_uri()},
            "position": {"line": line - 1, "character": utf16_column(row, column)},
        }


@asynccontextmanager
async def lsp_session(
    server: Path, root: Path, *, name: str = "language server"
) -> AsyncGenerator[LspSession]:
    """Start one language server, initialize it, and always stop it."""
    process = await asyncio.create_subprocess_exec(
        str(server),
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        process.kill()
        await process.wait()
        raise OSError(f"{name} started without usable pipes")
    session = LspSession(stdin, stdout, name)
    try:
        await session.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root.as_uri(),
                # Declared, not empty: a server answers `documentSymbol` with
                # the flat shape unless the client says it understands the
                # nested one, and the two are not interchangeable.
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}
                    }
                },
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
            },
        )
        await session.notify("initialized", {})
        yield session
        await session.request("shutdown", {})
        await session.notify("exit", {})
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
