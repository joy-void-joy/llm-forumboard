"""Concrete session/turn orchestration from semantic callbacks and capabilities."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeIs

from pydantic import BaseModel, Field

from lup.runtime.contracts import (
    EventStream,
    Interrupt,
    Session,
    Steer,
    SubmittedOutputStore,
    Turn,
    TurnToolBinder,
)
from lup.runtime.errors import (
    ProviderTurnError,
    StructuredOutputError,
    TurnAbortedError,
    TurnAlreadyActiveError,
    TurnError,
    TurnFailure,
)
from lup.runtime.models import (
    AnyTurnBlock,
    TurnHandle,
    TurnIdentifiers,
    TurnMessage,
    TurnRequest,
    TurnResult,
    SubmissionGate,
    SubmissionDecision,
    SubmissionGateResolver,
    TurnToolBinding,
)
from lup.runtime.output import InMemorySubmittedOutputStore, submission_history
from lup.types import Usage


class CompletedTurn(BaseModel, frozen=True):
    """Native-neutral completed evidence before typed submission assembly."""

    messages: list[TurnMessage] = []
    blocks: list[AnyTurnBlock] = []
    usage: Usage = Field(default_factory=Usage)
    duration: timedelta = timedelta()
    identifiers: TurnIdentifiers | None = Field(
        default=None,
        description=(
            "Identifiers as the provider settled them during the turn — a "
            "runtime that adopts the persisted session id reports it here; "
            "None means the accepted identifiers stand"
        ),
    )


type CompleteTurn = Callable[[], Awaitable[CompletedTurn]]


class AcceptedTurn(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Acknowledged native turn and independently supplied optional capabilities."""

    identifiers: TurnIdentifiers
    complete: CompleteTurn
    events: EventStream | None = None
    interrupt: Interrupt | None = None
    steer: Steer | None = None


type TurnStarter = Callable[[str], Awaitable[AcceptedTurn]]
type TurnFinished = Callable[[], None]
type OutputStoreFactory = Callable[[], SubmittedOutputStore]


class TurnLifecycle:
    """Mutable ownership state shared by one session and its active turn."""

    def __init__(self) -> None:
        self.aborted = False


def is_output_model(
    output_type: type[BaseModel] | type[None] | None,
) -> TypeIs[type[BaseModel]]:
    """Narrow Pydantic's generic ``type[T] | None`` construction field."""
    return output_type is not None and issubclass(output_type, BaseModel)


def refusal_of(blocks: list[AnyTurnBlock], tool: str) -> str | None:
    """Why *tool* was refused this turn, if it was called and refused.

    An empty output store reads the same whether the model never submitted
    or was blocked from submitting, and only the first is worth another
    prompt. The refusal's own text is the reason to report, because the
    generic message sends whoever reads it looking for a model mistake.
    """
    calls = {
        block.invoked_call_id
        for block in blocks
        if block.tool_call_name == tool and block.invoked_call_id is not None
    }
    refusals = (block.refusal for block in blocks)
    return next(
        (
            refusal.detail
            for refusal in refusals
            if refusal is not None and refusal.call_id in calls
        ),
        None,
    )


class ComposedTurn[T: BaseModel | None](Turn[T]):
    """Assemble native completion and the turn-local validated submission."""

    def __init__(
        self,
        accepted: AcceptedTurn,
        request: TurnRequest[T],
        store: SubmittedOutputStore | None,
        finished: TurnFinished,
        lifecycle: TurnLifecycle,
        submission_tool: str,
    ) -> None:
        self.accepted = accepted
        self.request = request
        self.store = store
        self.finished = finished
        self.lifecycle = lifecycle
        self.submission_tool = submission_tool
        self.resolved = False

    async def result(self) -> TurnResult[T]:
        if self.resolved:
            raise RuntimeError("turn result can only be consumed once")
        self.resolved = True
        completed: CompletedTurn | None = None
        try:
            if self.lifecycle.aborted:
                raise TurnAbortedError(
                    TurnFailure(
                        message="session closed before the turn completed",
                        identifiers=self.accepted.identifiers,
                    )
                )
            completed = await self.accepted.complete()
            if self.lifecycle.aborted:
                raise TurnAbortedError(
                    TurnFailure(
                        message="session closed before the turn completed",
                        blocks=completed.blocks,
                        usage=completed.usage,
                        duration=completed.duration,
                        identifiers=self.accepted.identifiers,
                    )
                )
            output_type = self.request.output_type
            if not is_output_model(output_type):
                output = None
            else:
                if self.store is None:
                    raise RuntimeError("required output turn has no binding")
                submitted = self.store.read(output_type)
                if submitted is None:
                    refused = refusal_of(completed.blocks, self.submission_tool)
                    raise StructuredOutputError(
                        TurnFailure(
                            message=(
                                "turn completed without a valid submit_output call"
                                if refused is None
                                else f"{self.submission_tool} was refused: {refused}"
                            ),
                            blocks=completed.blocks,
                            usage=completed.usage,
                            duration=completed.duration,
                            identifiers=self.accepted.identifiers,
                            validation_history=submission_history(self.store),
                            correctable=refused is None,
                        )
                    )
                output = submitted
            return TurnResult[T].model_validate(
                {
                    "output": output,
                    "messages": completed.messages,
                    "blocks": completed.blocks,
                    "usage": completed.usage,
                    "duration": completed.duration,
                    "identifiers": completed.identifiers or self.accepted.identifiers,
                }
            )
        except TurnError:
            raise
        except Exception as error:
            failure = TurnFailure(
                message=(
                    "session closed before the turn completed"
                    if self.lifecycle.aborted
                    else str(error)
                ),
                blocks=completed.blocks if completed is not None else [],
                usage=completed.usage if completed is not None else Usage(),
                duration=completed.duration if completed is not None else timedelta(),
                identifiers=self.accepted.identifiers,
            )
            if self.lifecycle.aborted:
                raise TurnAbortedError(failure) from error
            raise ProviderTurnError(failure) from error
        finally:
            self.finished()


class ComposedSession(Session):
    """Enforce binding-before-acceptance and one active turn."""

    def __init__(
        self,
        starter: TurnStarter,
        binder: TurnToolBinder,
        store_factory: OutputStoreFactory | None = None,
        gate_resolver: SubmissionGateResolver | None = None,
        submission_tool: str = "submit_output",
    ) -> None:
        self.starter = starter
        self.binder = binder
        self.submission_tool = submission_tool
        self.store_factory = store_factory or InMemorySubmittedOutputStore
        self.gate_resolver = gate_resolver
        self.active = False
        self.active_lifecycle: TurnLifecycle | None = None
        self.active_interrupt: Interrupt | None = None

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        if self.active:
            raise TurnAlreadyActiveError("session already has an active turn")
        self.active = True
        accepted: AcceptedTurn | None = None
        try:
            store: SubmittedOutputStore | None = None
            output_type = request.output_type
            if not is_output_model(output_type):
                await self.binder.bind(None)
            else:
                store = await bind_output(
                    self.binder,
                    output_type,
                    self.gate_resolver,
                    self.store_factory,
                )
            accepted = await self.starter(request.input.text)
        finally:
            if accepted is None:
                self.active = False
        if accepted is None:
            raise RuntimeError("turn acceptance ended without a native turn")
        lifecycle = TurnLifecycle()
        self.active_lifecycle = lifecycle
        self.active_interrupt = accepted.interrupt

        def finished() -> None:
            if self.active_lifecycle is not lifecycle:
                return
            self.active = False
            self.active_lifecycle = None
            self.active_interrupt = None

        turn = ComposedTurn[T](
            accepted, request, store, finished, lifecycle, self.submission_tool
        )
        return TurnHandle[T](
            turn=turn,
            events=accepted.events,
            interrupt=accepted.interrupt,
            steer=accepted.steer,
        )

    async def abort_active(self) -> None:
        """Abort an unfinished turn during session context exit."""
        lifecycle = self.active_lifecycle
        if lifecycle is None:
            return
        lifecycle.aborted = True
        interrupt = self.active_interrupt
        self.active = False
        self.active_lifecycle = None
        self.active_interrupt = None
        if interrupt is not None:
            await interrupt.interrupt()


async def bind_output[T: BaseModel](
    binder: TurnToolBinder,
    output_type: type[T],
    gate_resolver: SubmissionGateResolver | None,
    store_factory: OutputStoreFactory = InMemorySubmittedOutputStore,
) -> SubmittedOutputStore:
    """Create a fresh store and preserve T through the generic binder call."""
    store = store_factory()
    # A gate that accepts the base accepts this turn's T, so the resolved one
    # is installed as it is rather than wrapped in a closure that only forwards.
    gate = gate_resolver(output_type) if gate_resolver is not None else None
    binding = TurnToolBinding[T](output_type=output_type, store=store, gate=gate)
    await binder.bind(binding)
    return store


def submission_gate_resolver[T: BaseModel](
    output_type: type[T], gate: SubmissionGate[T]
) -> SubmissionGateResolver:
    """Resolve one typed submission gate without putting it on turn input.

    The erasure below is the cost of that choice, not a necessity: a session
    is configured before any turn names an output type, so the lookup is
    dynamic and `candidate is output_type` refines nothing for the checker.
    Revalidating earns the narrowing a `cast` would merely assert. Moving the
    gate onto `TurnRequest` beside `output_type` would remove it entirely.
    """

    def resolve(candidate: type[BaseModel]) -> SubmissionGate[BaseModel] | None:
        if candidate is not output_type:
            return None

        async def erased(
            value: BaseModel,  # lup: ignore[bare-basemodel] — the dynamic lookup above
        ) -> SubmissionDecision:
            typed = output_type.model_validate(value.model_dump(mode="json"))
            return await gate(typed)

        return erased

    return resolve
