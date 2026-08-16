"""Live ingestion of observable native CLI transcripts.

Claude Code and Codex own their interactive terminal UI, so Lup cannot wrap
their provider transport the way it wraps an SDK session. Both persist JSONL
session events, which is the seam: this watcher follows only bytes written
after launch and mirrors them into the same durable journal an SDK run writes,
so one transcript covers both ways of running an agent.

What a runtime writes, and where, is the runtime's own business. This module
holds the reading — polling, cursors, partial lines, scope — and asks a
:class:`NativeTranscripts` for everything vendor-shaped: where its sessions
live, how one of its records names the directory it belongs to, and what its
own words for a reasoning or tool block are. A merged table here would read
one vendor's records with the other's vocabulary, and would have to be edited
rather than extended when a third runtime arrives.

One watcher follows one runtime. A run that drives both starts two, which is
why nothing here has to decide whose record it is holding.
"""

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.telemetry.journal import ObservableEventKind, TraceJournal
from lup.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)

JSON_OBJECT = TypeAdapter(JsonObject)


class NativeSemanticBlock(BaseModel, frozen=True):
    """One semantic index projected from an exact vendor event."""

    kind: ObservableEventKind
    block: JsonObject


def blocks_by_type(
    record: JsonValue, spellings: Mapping[str, ObservableEventKind]
) -> list[NativeSemanticBlock]:
    """Every block in one record whose own ``type`` a runtime declares.

    Searched by descent rather than at a known path, because a runtime nests
    the same block under different envelopes and moves them between versions.
    The block's own ``type`` is what both keep stable, so an implementation
    supplies the words and this supplies the walk.
    """

    def walk(node: JsonValue) -> Iterator[NativeSemanticBlock]:
        match node:
            case dict() as mapping:
                native_type = mapping.get("type")  # lup: ignore[dict-get]
                if isinstance(native_type, str) and native_type in spellings:
                    yield NativeSemanticBlock(
                        kind=spellings[native_type], block=mapping
                    )
                for child in mapping.values():
                    yield from walk(child)
            case list() as items:
                for child in items:
                    yield from walk(child)
            case _:
                return

    return list(walk(record))


def first_string(record: JsonValue, key: str) -> str | None:
    """The first string found under ``key`` anywhere in one record.

    One runtime stamps the field at the top level and another carries it
    inside an opening metadata payload; a descent reaches both without either
    having to describe its envelope.
    """
    match record:
        case dict() as mapping:
            candidate = mapping.get(key)  # lup: ignore[dict-get]
            if isinstance(candidate, str):
                return candidate
            for child in mapping.values():
                if (found := first_string(child, key)) is not None:
                    return found
        case list() as items:
            for child in items:
                if (found := first_string(child, key)) is not None:
                    return found
        case _:
            return None
    return None


class NativeTranscripts(ABC):
    """What one runtime writes about a session, and how to read it.

    An engine the watcher is handed, never a surface a consumer holds: a
    caller wants a watcher, and the runtime it follows is a construction
    detail. Every method here is a place a vendor's own vocabulary would
    otherwise have leaked into provider-neutral code.
    """

    @abstractmethod
    def roots(self) -> list[Path]:
        """Directories under which this runtime persists session transcripts."""

    @abstractmethod
    def origin(self, record: JsonObject) -> Path | None:
        """The working directory this record belongs to, if it says.

        Returning ``None`` keeps the transcript out of a scoped run until a
        later record identifies it, which is the safe direction: guessing is
        how another project's session lands in this project's record.
        """

    @abstractmethod
    def semantic_blocks(self, record: JsonObject) -> list[NativeSemanticBlock]:
        """The reasoning and tool blocks this record exposes, in its own words."""


class NativeTranscriptWatcher:
    """Follow new JSONL bytes under one runtime's session roots until stopped."""

    def __init__(
        self,
        transcripts: NativeTranscripts,
        journal: TraceJournal,
        *,
        scope: Path | None = None,
        poll_seconds: float = 0.25,
    ) -> None:
        self.transcripts = transcripts
        self.journal = journal
        self.scope = scope
        self.poll_seconds = poll_seconds
        self.cursors: dict[Path, int] = {}
        self.origins: dict[Path, Path] = {}
        self.stop_signal = threading.Event()
        self.thread: threading.Thread | None = None

    def discover(self) -> list[Path]:
        """Find session JSONL files under every root this runtime declares."""
        return sorted(
            {
                path
                for root in self.transcripts.roots()
                if root.exists()
                for path in root.rglob("*.jsonl")
                if path.is_file()
            }
        )

    def snapshot(self) -> None:
        """Mark all pre-launch transcript bytes as historical.

        Taken before the first poll so a long-lived config root does not
        replay months of unrelated sessions into this run's journal.
        """
        self.cursors = {path: path.stat().st_size for path in self.discover()}

    def start(self) -> None:
        """Snapshot history and begin live background ingestion."""
        self.snapshot()
        self.thread = threading.Thread(
            target=self.watch,
            name="lup-native-transcript",
            daemon=True,
        )
        self.thread.start()

    def watch(self) -> None:
        """Poll native roots and ingest complete newly appended lines."""
        while not self.stop_signal.wait(self.poll_seconds):
            try:
                self.scan()
            except Exception as error:
                logger.exception("Native transcript ingestion failed")
                self.journal.emit("error", {"message": str(error)})

    def scan(self, *, final: bool = False) -> None:
        """Ingest all currently available complete records.

        A partial trailing line is left for the next pass, since the CLI is
        still writing it — except on the final scan, where nothing more is
        coming and a truncated record is better evidence than none.
        """
        for path in self.discover():
            cursor = self.cursors.get(path, 0)  # lup: ignore[dict-get]
            size = path.stat().st_size
            if size < cursor:
                cursor = 0
            if size == cursor:
                continue
            with path.open("rb") as stream:
                stream.seek(cursor)
                appended = stream.read()
            offset = 0
            for line in appended.splitlines(keepends=True):
                if not line.endswith((b"\n", b"\r")) and not final:
                    break
                offset += len(line)
                self.record_line(path, line.removesuffix(b"\n").removesuffix(b"\r"))
            self.cursors[path] = cursor + offset

    def in_scope(self, path: Path) -> bool:
        """Report whether a transcript belongs to the scoped project."""
        if self.scope is None:
            return True
        origin = self.origins.get(path)  # lup: ignore[dict-get]
        return origin is not None and origin.is_relative_to(self.scope)

    def record_line(self, path: Path, encoded: bytes) -> None:
        """Persist one vendor event and semantic indexes for exposed blocks."""
        if not encoded:
            return
        source = str(path)
        try:
            record = JSON_OBJECT.validate_json(encoded)
        except ValidationError:
            if self.in_scope(path):
                self.journal.emit(
                    "message",
                    {
                        "native_source": source,
                        "raw": encoded.decode("utf-8", errors="replace"),
                    },
                )
            return
        if (origin := self.transcripts.origin(record)) is not None:
            self.origins[path] = origin
        if not self.in_scope(path):
            return
        self.journal.emit("message", {"native_source": source, "native_event": record})
        for semantic in self.transcripts.semantic_blocks(record):
            self.journal.emit(
                semantic.kind,
                {"native_source": source, "block": semantic.block},
            )

    def stop(self) -> None:
        """Stop the watcher and synchronously ingest the final partial record."""
        self.stop_signal.set()
        if self.thread is not None:
            self.thread.join()
        self.scan(final=True)
