# lup: ignore[own-model-dispatch]
# Which journal event a turn event becomes is this layer's reading of it, not
# something the event knows about itself: `ObservableEventKind` is telemetry's
# vocabulary, and declaring the projection on the runtime event union would
# have `lup.runtime` depend on `lup.telemetry`, inverting the one direction
# these two layers are allowed to point in.
"""Complete observable, hierarchical runtime transcripts.

The ordinary :mod:`lup.telemetry.trace` sidecar is a compact feedback index —
what a later reader skims to find a session worth opening. This is the other
half: the lossless audit stream, where every event the provider or runtime
exposes is appended immediately, correlated to the actor and span that
produced it, and chained by hash so a gap or an edit is visible rather than
silent.

Three properties are deliberate. It appends *as it happens*, because a
transcript assembled at the end is missing exactly the runs that crashed. It
is chained, so the record can be checked rather than trusted. And it never
claims to recover reasoning a provider keeps private — an absent thought is
absent from the journal too, rather than reconstructed into something that
reads like evidence.
"""

import asyncio
import fcntl
import hashlib
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from lup.runtime.composition import is_output_model
from lup.runtime.contracts import EventStream, Session, Turn
from lup.runtime.errors import TurnError
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    BlockCompletedEvent,
    BlockDeltaEvent,
    BlockStartedEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionHandle,
    SessionId,
    TurnCompletedEvent,
    TurnEvent,
    TurnHandle,
    TurnMessage,
    TurnRequest,
    TurnResult,
    TurnStartedEvent,
    UNNAMED_SUBAGENT,
)
from lup.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = 1
BLOB_THRESHOLD_BYTES = 64 * 1024
# lup: ignore[constant-declaration] — the marker this repository writes into a
# transcript in place of a secret, which a reader recognizes by sight
REDACTED = "[REDACTED]"

# Matched as substrings of a field name, so one fragment covers the spellings
# a provider might pick ("api_key", "x-api-key", "apiKeyId"). Over-matching
# costs a redacted field somebody wanted; under-matching writes a credential
# into a durable file, which is the direction worth erring away from.
SECRET_KEY_FRAGMENTS = (
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "session_token",
)

type ActorKind = Literal[
    "harness",
    "orchestrator",
    "native_agent",
    "native_subagent",
    "nested_agent",
    "background_agent",
    "tool",
    "job",
    "system",
]
type ObservableEventKind = Literal[
    "run_start",
    "run_end",
    "session_start",
    "session_end",
    "turn_input",
    "turn_start",
    "block_start",
    "block_delta",
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "turn_result",
    "turn_end",
    "subagent_start",
    "subagent_end",
    "job_state",
    "sleep",
    "wake",
    "artifact",
    "derived_status",
    "usage",
    "error",
]


class TraceActor(BaseModel, frozen=True):
    """One participant in the observable agent tree."""

    kind: ActorKind
    name: str
    provider: str | None = None
    model: str | None = None


class TraceContext(BaseModel, frozen=True):
    """Stable run and parentage identifiers inherited by emitted events."""

    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    actor: TraceActor

    @classmethod
    def root(cls, run_id: str, actor: TraceActor) -> "TraceContext":
        """Create the root span for one run."""
        return cls(
            run_id=run_id,
            trace_id=uuid4().hex,
            span_id=uuid4().hex,
            actor=actor,
        )

    def child(self, actor: TraceActor) -> "TraceContext":
        """Create a correlated child agent, tool, or job span."""
        return TraceContext(
            run_id=self.run_id,
            trace_id=self.trace_id,
            span_id=uuid4().hex,
            parent_span_id=self.span_id,
            actor=actor,
        )


class ObservableEventDraft(BaseModel, frozen=True):
    """Hash input for one event, excluding the chain-derived digest."""

    schema_version: int = TRACE_SCHEMA_VERSION
    sequence: int = Field(ge=0)
    timestamp: str
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    actor: TraceActor
    session_id: str | None = None
    turn_id: str | None = None
    kind: ObservableEventKind
    payload: JsonObject = {}
    previous_hash: str | None = None

    def digest(self) -> str:
        """Hash this canonical draft, including the prior chain hash."""
        encoded = self.model_dump_json(by_alias=True, exclude_none=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ObservableEvent(ObservableEventDraft, frozen=True):
    """One persisted event in a tamper-evident transcript."""

    event_hash: str

    @property
    def tool_name(self) -> str | None:
        """Tool leaf name for a canonical tool-actor event, else ``None``.

        Actor names are dotted, so the leaf is what a caller means when it
        asks which tool ran.
        """
        if self.actor.kind != "tool":
            return None
        separator = self.actor.name.rfind(".")
        return self.actor.name[separator + 1 :]


def bare_name(text: str) -> str:
    """One field name reduced to what every spelling of it has in common."""
    return "".join(letter for letter in text.casefold() if letter not in "-_")


class Redaction(ABC):
    """One way of removing credential material from a payload.

    An engine rather than a surface: a journal is handed one and applies it,
    and callers hold the journal. Keeping it abstract is what lets a
    deployment add a rule — masking a command line, scanning values for
    secret-shaped substrings — by composing another rule in, rather than by
    editing a walk that every other rule also runs through.
    """

    @abstractmethod
    def apply(self, value: JsonValue) -> JsonValue:
        """Return ``value`` with whatever this rule redacts replaced."""


class KeyRedaction(Redaction):
    """Redact a value whose own field name denotes credential material.

    The common case, and the cheap one: a payload that labels its secret is
    one lookup away from being safe. It says nothing about values, so a
    credential sitting under an innocuous name passes through — that is what
    a second, composed rule is for.
    """

    def __init__(self, fragments: Sequence[str] = SECRET_KEY_FRAGMENTS) -> None:
        self.fragments = tuple(fragments)

    def names_a_secret(self, key: str) -> bool:
        """Whether a structured field name denotes credential material.

        Separators are dropped from both sides before matching, because the
        same credential is spelled three ways depending on where it is
        written — ``api_key`` in JSON, ``--api-key`` on a command line,
        ``apiKey`` in a header — and all three name the same thing.
        """
        folded = bare_name(key)
        return any(bare_name(fragment) in folded for fragment in self.fragments)

    def apply(self, value: JsonValue) -> JsonValue:
        match value:
            case dict() as mapping:
                return {
                    str(key): (
                        REDACTED if self.names_a_secret(str(key)) else self.apply(child)
                    )
                    for key, child in mapping.items()
                }
            case list() as items:
                return [self.apply(item) for item in items]
            case _:
                return value


class ArgvRedaction(Redaction):
    """Redact credential values in a command line, named by the flag beside them.

    The one place :class:`KeyRedaction` cannot help: the secret is a value,
    and what identifies it is the flag before it rather than a key above it.
    Both spellings carry one — ``--api-key=X`` in the same word, ``--api-key
    X`` in the next — so the fold remembers whether the argument it is about
    to see is a secret.

    Applied only to a vector a caller hands over as a command line. It is not
    in the journal's default chain, because most lists of strings in a
    payload are not argv and reading them as argv would mangle them.
    """

    def __init__(self, fragments: Sequence[str] = SECRET_KEY_FRAGMENTS) -> None:
        self.keys = KeyRedaction(fragments)

    def arguments(self, argv: Sequence[str]) -> list[str]:
        """Redact one argv vector while preserving its shape."""

        def folded() -> Iterator[str]:
            redact_next = False
            for argument in argv:
                if redact_next:
                    redact_next = False
                    yield REDACTED
                    continue
                # lup: ignore[string-split] — `--flag=value` is argv's own
                # syntax, and no standard-library parser reads a command line
                option, separator, _value = argument.partition("=")
                if not (option.startswith("-") and self.keys.names_a_secret(option)):
                    yield argument
                    continue
                if separator:
                    yield f"{option}={REDACTED}"
                else:
                    redact_next = True
                    yield argument

        return list(folded())

    def apply(self, value: JsonValue) -> JsonValue:
        match value:
            case list() as items if all(isinstance(item, str) for item in items):
                return list(self.arguments([str(item) for item in items]))
            case _:
                return value


class Redactions(Redaction):
    """Apply several redactions in order, each over the last one's result."""

    def __init__(self, *rules: Redaction) -> None:
        self.rules = rules

    def apply(self, value: JsonValue) -> JsonValue:
        for rule in self.rules:
            value = rule.apply(value)
        return value


def store_large_values(value: JsonValue, directory: Path) -> JsonValue:
    """Move large strings into content-addressed blobs without losing bytes.

    A single oversized payload would otherwise make every later line of the
    transcript expensive to scan past. The bytes are kept, addressed by their
    own digest, so the reference is stable and identical payloads collapse.
    """
    match value:
        case str() as text if len(text.encode("utf-8")) >= BLOB_THRESHOLD_BYTES:
            encoded = text.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{digest}.txt"
            if not destination.exists():
                destination.write_bytes(encoded)
            return {
                "$blob": digest,
                "media_type": "text/plain; charset=utf-8",
                "path": str(destination),
                "size_bytes": len(encoded),
            }
        case dict() as mapping:
            return {
                str(key): store_large_values(child, directory)
                for key, child in mapping.items()
            }
        case list() as items:
            return [store_large_values(item, directory) for item in items]
        case _:
            return value


class JournalState:
    """Shared append cursor and lock for every actor in one trace tree."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        existing = read_observable_events(path) if path.exists() else []
        self.sequence = len(existing)
        self.previous_hash = existing[-1].event_hash if existing else None
        self.lock = threading.Lock()


class TraceJournal:
    """Append complete observable events immediately and durably."""

    def __init__(
        self,
        path: Path,
        context: TraceContext,
        state: JournalState | None = None,
        redaction: Redaction | None = None,
    ) -> None:
        self.context = context
        self.state = state or JournalState(path)
        self.redaction = redaction or KeyRedaction()

    @property
    def path(self) -> Path:
        """The shared JSONL path this actor span appends to."""
        return self.state.path

    def child(self, actor: TraceActor) -> "TraceJournal":
        """Write a child span into the same canonical event stream."""
        return TraceJournal(
            self.path, self.context.child(actor), self.state, self.redaction
        )

    def emit(
        self,
        kind: ObservableEventKind,
        payload: JsonObject | None = None,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> ObservableEvent:
        """Redact, chain, append, flush, and return one event.

        The chain head is re-read from the file inside the lock rather than
        trusted from memory, because a second process appending to the same
        transcript would otherwise leave both writers chaining from a head
        that is no longer last.
        """
        with self.state.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.state.lock_path.open("a", encoding="utf-8") as lock_stream:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
                try:
                    return self.append(kind, payload, session_id, turn_id)
                finally:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    def append(
        self,
        kind: ObservableEventKind,
        payload: JsonObject | None,
        session_id: str | None,
        turn_id: str | None,
    ) -> ObservableEvent:
        """Chain and write one event, with the transcript lock already held."""
        latest = read_last_observable_event(self.path)
        self.state.sequence = latest.sequence + 1 if latest else 0
        self.state.previous_hash = latest.event_hash if latest else None
        safe_payload = self.redaction.apply(payload or {})
        if not isinstance(safe_payload, dict):
            raise TypeError("event payload redaction changed object shape")
        persisted = store_large_values(safe_payload, self.path.with_suffix(".blobs"))
        if not isinstance(persisted, dict):
            raise TypeError("event payload storage changed object shape")
        draft = ObservableEventDraft(
            sequence=self.state.sequence,
            timestamp=datetime.now().astimezone().isoformat(),
            run_id=self.context.run_id,
            trace_id=self.context.trace_id,
            span_id=self.context.span_id,
            parent_span_id=self.context.parent_span_id,
            actor=self.context.actor,
            session_id=session_id,
            turn_id=turn_id,
            kind=kind,
            payload=persisted,
            previous_hash=self.state.previous_hash,
        )
        event = ObservableEvent(
            **draft.model_dump(mode="python"),
            event_hash=draft.digest(),
        )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.state.sequence += 1
        self.state.previous_hash = event.event_hash
        return event


def read_last_observable_event(path: Path) -> ObservableEvent | None:
    """Read only the final complete JSONL record.

    Seeking to the last line rather than parsing the file is what keeps the
    per-event cost flat: a transcript is appended to once per event, and
    re-reading all of it each time would make a long run quadratic.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell() - 1
        while position >= 0:
            stream.seek(position)
            if stream.read(1) != b"\n":
                break
            position -= 1
        while position >= 0:
            stream.seek(position)
            if stream.read(1) == b"\n":
                position += 1
                break
            position -= 1
        stream.seek(max(position, 0))
        line = stream.readline().decode("utf-8")
    if not line:
        return None
    return ObservableEvent.model_validate_json(line)


def read_observable_events(path: Path) -> list[ObservableEvent]:
    """Load every valid record from an observable JSONL transcript.

    A malformed line is reported and skipped rather than ending the read: a
    run killed mid-write leaves a torn final line, and the events before it
    are still the evidence somebody opened the file for.
    """
    if not path.exists():
        return []
    events: list[ObservableEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            events.append(ObservableEvent.model_validate_json(line))
        except ValueError:
            logger.exception("Skipping malformed observable trace line in %s", path)
    return events


def verify_event_chain(events: list[ObservableEvent]) -> bool:
    """Check sequence, prior-hash linkage, and every event digest."""
    prior: str | None = None
    for sequence, event in enumerate(events):
        if event.sequence != sequence or event.previous_hash != prior:
            return False
        draft = ObservableEventDraft.model_validate(
            event.model_dump(exclude={"event_hash"})
        )
        if event.event_hash != draft.digest():
            return False
        prior = event.event_hash
    return True


def block_event_kind(block_type: str) -> ObservableEventKind:
    """Which canonical event kind one completed block is recorded as."""
    match block_type:
        case "thinking":
            return "reasoning"
        case "tool_call":
            return "tool_call"
        case "tool_result":
            return "tool_result"
        case _:
            return "message"


class TurnRecorder:
    """Project one turn's live events into the journal as they arrive.

    Stateful because native delegation reaches us in two halves that the
    provider never joins: the delegating call names the role, and the
    messages produced under it carry only that call's id. The call always
    streams past first, so remembering it as it goes is enough — and it
    keeps the delegated spans written *during* the turn, which is the whole
    point of an append-as-it-happens transcript. Reconstructing them from
    the finished result instead would lose exactly the turns that died.
    """

    def __init__(self, journal: TraceJournal) -> None:
        self.journal = journal
        # lup: ignore[dict-str-payload] — keyed by provider-minted call ids
        self.roles: dict[str, str] = {}
        self.children: dict[str, TraceJournal] = {}

    def child_for(self, call_id: str, model: str | None) -> TraceJournal:
        """The child span for one delegation, opened on first sight."""
        if call_id not in self.children:
            role = self.roles.get(call_id, UNNAMED_SUBAGENT)  # lup: ignore[dict-get]
            child = self.journal.child(
                TraceActor(
                    kind="native_subagent",
                    name=role,
                    provider=self.journal.context.actor.provider,
                    model=model,
                )
            )
            self.children[call_id] = child
            child.emit("subagent_start", {"parent_tool_call_id": call_id})
        return self.children[call_id]

    def record(self, event: LiveTurnEvent) -> None:
        """Journal one live event, opening delegated spans as they appear."""
        session_id = event.identifiers.session.value
        turn_id = event.identifiers.turn.value
        match event:
            case TurnStartedEvent():
                self.journal.emit("turn_start", session_id=session_id, turn_id=turn_id)
            case BlockStartedEvent(block=block):
                self.journal.emit(
                    "block_start",
                    {"block": block.model_dump(mode="json")},
                    session_id=session_id,
                    turn_id=turn_id,
                )
            case BlockDeltaEvent(delta=delta):
                self.journal.emit(
                    "block_delta",
                    {"delta": delta},
                    session_id=session_id,
                    turn_id=turn_id,
                )
            case BlockCompletedEvent(block=block):
                role = block.delegated_role()
                call_id = block.invoked_call_id
                if role is not None and call_id is not None:
                    self.roles[call_id] = role
                self.journal.emit(
                    block_event_kind(block.type),
                    {"block": block.model_dump(mode="json")},
                    session_id=session_id,
                    turn_id=turn_id,
                )
            case MessageCompletedEvent(message=message):
                self.record_message(message, session_id, turn_id)
            case TurnCompletedEvent():
                self.close(session_id, turn_id)
                self.journal.emit("turn_end", session_id=session_id, turn_id=turn_id)

    def record_message(
        self, message: TurnMessage, session_id: str, turn_id: str
    ) -> None:
        """Attribute a delegated message to its own span.

        Only delegated messages are recorded here. The parent's own blocks
        already went by individually, and repeating them at message
        granularity would double every line of the transcript.
        """
        call_id = message.parent_tool_call_id
        if call_id is None:
            return
        child = self.child_for(call_id, message.model)
        for block in message.blocks:
            child.emit(
                block_event_kind(block.type),
                {
                    "role": message.role,
                    "message_id": message.message_id,
                    "block": block.model_dump(mode="json"),
                },
                session_id=session_id,
                turn_id=turn_id,
            )

    def close(self, session_id: str, turn_id: str) -> None:
        """Close every delegated span still open at the end of the turn."""
        for call_id, child in self.children.items():
            child.emit(
                "subagent_end",
                {"parent_tool_call_id": call_id},
                session_id=session_id,
                turn_id=turn_id,
            )
        self.children.clear()


class JournalEventStream(EventStream):
    """Drain one native stream while mirroring it to callers and the journal.

    The source is consumed exactly once, by this class, because a provider
    stream cannot be replayed: recording it and handing it on have to be the
    same pass, or one of the two gets nothing.
    """

    def __init__(
        self,
        inner: EventStream,
        journal: TraceJournal,
        mirrored: asyncio.Queue[LiveTurnEvent | None],
    ) -> None:
        self.inner = inner
        self.recorder = TurnRecorder(journal)
        self.journal = journal
        self.queue = mirrored
        self.consumed = False
        self.drain = asyncio.create_task(self.drain_inner())

    async def drain_inner(self) -> None:
        """Record and mirror the complete source stream exactly once."""
        try:
            async for event in self.inner.live():
                self.recorder.record(event)
                self.queue.put_nowait(event)
        except Exception as error:
            self.journal.emit("error", {"message": str(error)})
            raise
        finally:
            self.queue.put_nowait(None)

    async def wait(self) -> None:
        """Wait until the provider event stream reaches its terminal marker."""
        await self.drain

    async def iterate(self) -> AsyncIterator[LiveTurnEvent]:
        if self.consumed:
            raise RuntimeError("journal event stream can only be consumed once")
        self.consumed = True
        while (event := await self.queue.get()) is not None:
            yield event

    async def durable(self) -> AsyncIterator[TurnEvent]:
        async for event in self.iterate():
            if (kept := event.durable) is not None:
                yield kept

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        return self.iterate()


class JournalTurn[T: BaseModel | None](Turn[T]):
    """Persist the terminal result or complete failure of one logical turn."""

    def __init__(
        self,
        inner: Turn[T],
        journal: TraceJournal,
        event_stream: JournalEventStream | None,
    ) -> None:
        self.inner = inner
        self.journal = journal
        self.event_stream = event_stream

    async def result(self) -> TurnResult[T]:
        try:
            result = await self.inner.result()
        except TurnError as error:
            failure = error.failure
            if self.event_stream is not None:
                await self.event_stream.wait()
            identifiers = failure.identifiers
            self.journal.emit(
                "error",
                {
                    "message": failure.message,
                    "blocks": [
                        block.model_dump(mode="json") for block in failure.blocks
                    ],
                    "usage": failure.usage.model_dump(mode="json"),
                },
                session_id=(
                    identifiers.session.value if identifiers is not None else None
                ),
                turn_id=(identifiers.turn.value if identifiers is not None else None),
            )
            raise
        if self.event_stream is not None:
            await self.event_stream.wait()
        self.journal.emit(
            "turn_result",
            {
                "messages": [
                    message.model_dump(mode="json") for message in result.messages
                ],
                "blocks": [block.model_dump(mode="json") for block in result.blocks],
                "output": (
                    result.output.model_dump(mode="json")
                    if isinstance(result.output, BaseModel)
                    else None
                ),
                "usage": result.usage.model_dump(mode="json"),
                "duration_seconds": result.duration.total_seconds(),
            },
            session_id=result.identifiers.session.value,
            turn_id=result.identifiers.turn.value,
        )
        return result


class JournalSession(Session):
    """Record turn input and attach lossless event/result journaling."""

    def __init__(self, inner: Session, journal: TraceJournal) -> None:
        self.inner = inner
        self.journal = journal

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        self.journal.emit(
            "turn_input",
            {
                "text": request.input.text,
                "output_schema": (
                    request.output_type.model_json_schema()
                    if is_output_model(request.output_type)
                    else None
                ),
            },
        )
        handle = await self.inner.start(request)
        mirrored: asyncio.Queue[LiveTurnEvent | None] = asyncio.Queue()
        event_stream = (
            JournalEventStream(handle.events, self.journal, mirrored)
            if handle.events is not None
            else None
        )
        return TurnHandle[T](
            turn=JournalTurn(handle.turn, self.journal, event_stream),
            events=event_stream,
            interrupt=handle.interrupt,
            steer=handle.steer,
        )


def journal_session_factory(
    inner: SessionFactory, journal: TraceJournal
) -> SessionFactory:
    """Give every session opened by ``inner`` the canonical observable journal."""

    @asynccontextmanager
    async def open_journaled(
        resume: SessionId | None = None,
    ) -> AsyncGenerator[SessionHandle]:
        journal.emit(
            "session_start",
            {"resume_session_id": resume.value if resume is not None else None},
        )
        try:
            async with inner.open(resume) as handle:
                yield SessionHandle(
                    session=JournalSession(handle.session, journal),
                    fork=handle.fork,
                )
        except Exception as error:
            journal.emit("error", {"message": str(error)})
            raise
        finally:
            journal.emit("session_end")

    return SessionFactory(open_journaled)
