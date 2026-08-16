"""Immutable semantic values shared by all runtime implementations."""

import json
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated, Literal, Self, overload

from pydantic import BaseModel, Discriminator, Field

from lup.runtime.contracts import (
    EventStream,
    ForkSession,
    Interrupt,
    Session,
    Steer,
    SubmittedOutputStore,
    Turn,
)
from lup.types import (
    JsonObject,
    LupContentBlock,
    LupTextBlock,
    LupThinkingBlock,
    LupToolResultBlock,
    LupToolUseBlock,
    Usage,
)


# The tool names a runtime spells native delegation with. Kept beside the
# block that answers about a delegation so no reader has to know them.
DELEGATION_TOOLS = ("Agent", "Task")

# What a delegation is called when its call named no role.
UNNAMED_SUBAGENT = "subagent"


class SessionId(BaseModel, frozen=True):
    """Opaque native conversation identity."""

    value: str


class TurnId(BaseModel, frozen=True):
    """Opaque native turn identity."""

    value: str


class TurnIdentifiers(BaseModel, frozen=True):
    """Identities attached to one accepted turn."""

    session: SessionId
    turn: TurnId


class TurnInput(BaseModel, frozen=True):
    """Portable user input for one turn."""

    text: str


class TurnBlock(BaseModel, frozen=True):
    """One completed block of a turn, answering every question about itself.

    Whatever a caller needs to know about a block is declared here and
    answered — or declined — by the block, so a new kind of block is one class
    rather than an edit to every walk that would have to notice it. The
    declining answers are what make omission safe: a caller joining
    ``text_payload`` reaches every kind that carries prose, including kinds
    written long after the caller was.

    Pydantic's metaclass is an ``ABCMeta``, so ``telemetry_block`` binds like
    any abstract property: a kind that does not answer it cannot be built.
    """

    @property
    @abstractmethod
    def telemetry_block(self) -> LupContentBlock:
        """This block as the telemetry vocabulary spells it."""

    @property
    def text_payload(self) -> str | None:
        """Prose this block carries verbatim, if it carries any.

        Everything that reads what a turn actually said asks this instead of
        naming the kinds of block that hold text.
        """
        return None

    @property
    def tool_call_name(self) -> str | None:
        """The tool this block invokes, if it invokes one."""
        return None

    @property
    def tool_arguments(self) -> JsonObject | None:
        """The arguments this block invokes its tool with, if it invokes one."""
        return None

    @property
    def invoked_call_id(self) -> str | None:
        """The id of the call this block makes, if it makes one."""
        return None

    def delegated_role(
        self,
        tools: tuple[str, ...] = DELEGATION_TOOLS,
        unnamed: str = UNNAMED_SUBAGENT,
    ) -> str | None:
        """The subagent role this block delegates to, if it delegates.

        Asked of the block so a reader correlating a transcript never has to
        know which tool a runtime spells delegation with, nor which argument
        carries the role. Both are parameters because a runtime this library
        has not met spells them its own way, and a caller should not have to
        fork a block to say so.
        """
        return None

    @property
    def refusal(self) -> "ToolRefusal | None":
        """The refused call this block reports, if it reports one."""
        return None


class TurnTextBlock(TurnBlock, frozen=True):
    """One completed assistant text block."""

    type: Literal["text"] = "text"
    text: str

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupTextBlock(text=self.text)

    @property
    def text_payload(self) -> str | None:
        return self.text


class TurnThinkingBlock(TurnBlock, frozen=True):
    """One completed reasoning block."""

    type: Literal["thinking"] = "thinking"
    thinking: str
    redacted: bool = False

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupThinkingBlock(thinking=self.thinking, redacted=self.redacted)


class TurnToolCallBlock(TurnBlock, frozen=True):
    """One completed tool invocation."""

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: JsonObject = {}

    @property
    def telemetry_block(self) -> LupContentBlock:
        return LupToolUseBlock(id=self.id, name=self.name, input=self.arguments)

    @property
    def tool_call_name(self) -> str | None:
        return self.name

    @property
    def tool_arguments(self) -> JsonObject | None:
        return self.arguments

    @property
    def invoked_call_id(self) -> str | None:
        return self.id

    def delegated_role(
        self,
        tools: tuple[str, ...] = DELEGATION_TOOLS,
        unnamed: str = UNNAMED_SUBAGENT,
    ) -> str | None:
        if self.name not in tools:
            return None
        requested = self.arguments.get("subagent_type")  # lup: ignore[dict-get]
        if not isinstance(requested, str):
            requested = self.arguments.get("name")  # lup: ignore[dict-get]
        return requested if isinstance(requested, str) else unnamed


class ToolRefusal(BaseModel, frozen=True):
    """One tool call that returned an error instead of a result."""

    call_id: str
    detail: str


class TurnToolResultBlock(TurnBlock, frozen=True):
    """One completed tool result."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False

    @property
    def refusal(self) -> ToolRefusal | None:
        if not self.is_error:
            return None
        return ToolRefusal(call_id=self.tool_call_id, detail=self.content)

    @property
    def telemetry_block(self) -> LupContentBlock:
        rendered = (
            json.dumps({"is_error": True, "content": self.content})
            if self.is_error
            else self.content
        )
        return LupToolResultBlock(tool_use_id=self.tool_call_id, content=rendered)


type AnyTurnBlock = Annotated[
    TurnTextBlock | TurnThinkingBlock | TurnToolCallBlock | TurnToolResultBlock,
    Discriminator("type"),
]
"""One block as a pydantic *field* validates it: the closed set, discriminated.

Annotations that only read a block name :class:`TurnBlock`, the base. A field
must name this alias instead — validating against the base alone would rebuild
every block as a base instance and drop its payload.
"""


class TurnMessage(BaseModel, frozen=True):
    """A portable transcript message derived from canonical blocks."""

    role: Literal["user", "assistant", "tool", "system"]
    blocks: list[AnyTurnBlock]
    parent_tool_call_id: str | None = Field(
        default=None,
        description=(
            "The delegation tool call this message was produced under, when "
            "the provider attributes it to one — the only evidence that "
            "separates a native subagent's messages from its parent's"
        ),
    )
    model: str | None = Field(
        default=None,
        description="Model the provider reports for this message, when it does",
    )
    message_id: str | None = Field(
        default=None,
        description="Provider's own message identifier, for correlating replays",
    )


class TurnEventBase(BaseModel, frozen=True):
    """One thing that happened during a turn, answering about itself.

    The same arrangement :class:`TurnBlock` uses, for the same reason: a walk
    over events asks the event, so a new kind of event is one class rather
    than an edit to every filter that would have to notice it. The declining
    answers are what make omission safe — a caller folding ``completed_message``
    reaches every kind that carries one, including kinds written later.
    """

    @property
    def durable(self) -> "Self | None":
        """This event, if it survives into the transcript.

        Returning the event rather than a flag is what lets a caller keep the
        narrower type: a walk filtering on this gets exactly the durable
        kinds, the way naming them in an ``isinstance`` used to do. Only
        in-flight fragments decline, so the default is every terminal event's
        answer.
        """
        return self

    @property
    def completed_message(self) -> "TurnMessage | None":
        """The whole transcript message this event completed, if it completed one."""
        return None


class TurnStartedEvent(TurnEventBase, frozen=True):
    """A native turn was accepted."""

    type: Literal["turn_started"] = "turn_started"
    identifiers: TurnIdentifiers


class BlockStartedEvent(TurnEventBase, frozen=True):
    """A native content block started."""

    type: Literal["block_started"] = "block_started"
    identifiers: TurnIdentifiers
    block: AnyTurnBlock


class BlockDeltaEvent(TurnEventBase, frozen=True):
    """One text or thinking delta from an active native block."""

    type: Literal["block_delta"] = "block_delta"
    identifiers: TurnIdentifiers
    delta: str

    @property
    def durable(self) -> None:
        """A fragment of a block still being written survives nothing."""
        return None


class BlockCompletedEvent(TurnEventBase, frozen=True):
    """One native content block completed."""

    type: Literal["block_completed"] = "block_completed"
    identifiers: TurnIdentifiers
    block: AnyTurnBlock


class MessageCompletedEvent(TurnEventBase, frozen=True):
    """One whole transcript message completed.

    The message is carried rather than reconstructed. Folding loose blocks
    back into messages would need contiguous-role grouping, which silently
    merges two consecutive assistant messages into one; carrying the whole
    message makes the fold exact.
    """

    type: Literal["message_completed"] = "message_completed"
    identifiers: TurnIdentifiers
    message: TurnMessage

    @property
    def completed_message(self) -> TurnMessage:
        return self.message


class TurnCompletedEvent(TurnEventBase, frozen=True):
    """A native turn reached a terminal state."""

    type: Literal["turn_completed"] = "turn_completed"
    identifiers: TurnIdentifiers


type TurnEvent = (
    TurnStartedEvent
    | BlockStartedEvent
    | BlockCompletedEvent
    | MessageCompletedEvent
    | TurnCompletedEvent
)
"""Everything durable. A transcript folds from exactly these.

Deltas are deliberately absent: :func:`lup.runtime.transcript.fold_transcript`
takes this union, so a partial fragment cannot reach the fold at all rather
than being filtered out inside it.
"""

type LiveTurnEvent = TurnEvent | BlockDeltaEvent
"""Everything durable, plus in-flight deltas, in order.

A strict superset of :data:`TurnEvent`, so a consumer picks one accessor and
gets consistent behaviour either way instead of two views that disagree
about what happened.
"""


class SubmissionDecision(BaseModel, frozen=True):
    """A reflection gate's decision about validated output."""

    accepted: bool
    message: str = ""


type SubmissionGate[T] = Callable[[T], Awaitable[SubmissionDecision]]
type SubmissionGateResolver = Callable[
    [type[BaseModel]], SubmissionGate[BaseModel] | None
]


class TurnRequest[T: BaseModel | None](
    BaseModel, frozen=True, arbitrary_types_allowed=True
):
    """Per-turn input and optional validated output type."""

    input: TurnInput
    output_type: type[T] | None = None


# The overload pair and the implementation are one constructor, so each `def`
# answers for itself: `input` is the value being packaged, in either of the two
# spellings a caller may hand it, and the operation is building the request
# around it rather than anything a TurnInput does to itself.
# lup: ignore[model-free-function] — a constructor; TurnInput is what it packages
@overload
def turn_request(input: str | TurnInput) -> TurnRequest[None]: ...


# lup: ignore[model-free-function] — a constructor; TurnInput is what it packages
@overload
def turn_request[T: BaseModel](
    input: str | TurnInput,
    output_type: type[T],
) -> TurnRequest[T]: ...


# lup: ignore[model-free-function] — a constructor; TurnInput is what it packages
def turn_request[T: BaseModel](
    input: str | TurnInput,
    output_type: type[T] | None = None,
) -> TurnRequest[T] | TurnRequest[None]:
    """Construct a request while preserving its output type relationship.

    The overload pair is what preserves it. Collapsed into this single
    implementation signature, ``T`` is left unsolved when the argument is
    omitted, and pyright infers ``TurnRequest[Unknown] | TurnRequest[None]``
    there and ``TurnRequest[Summary] | TurnRequest[None]`` when a model is
    passed. The overloads pin each direction to one exact type.
    """
    # Narrowed on `str` rather than on `TurnInput`: the foreign alternative is
    # the one that cannot answer for itself, and asking about it leaves ours
    # to arrive by exclusion instead of by name.
    match input:
        case str():
            prompt = TurnInput(text=input)
        case _:
            prompt = input
    if output_type is None:
        return TurnRequest[None](input=prompt)
    return TurnRequest[T](input=prompt, output_type=output_type)


class TurnResult[T: BaseModel | None](BaseModel, frozen=True):
    """Successful terminal result; failures are represented only by errors."""

    output: T
    messages: list[TurnMessage]
    blocks: list[AnyTurnBlock]
    usage: Usage
    duration: timedelta
    identifiers: TurnIdentifiers


class SessionHandle(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Transparent composition of a session and optional fork capability.

    Reaching a capability through this handle is not a consumer holding an
    ABC: the handle carries capabilities and no behaviour of its own, so
    there is nothing for a composing surface to home. ``SessionFactory`` is
    the behavioural surface over these seams.
    """

    session: Session
    fork: ForkSession | None = None


class TurnHandle[T: BaseModel | None](
    BaseModel, frozen=True, arbitrary_types_allowed=True
):
    """Transparent composition of an accepted turn's capabilities.

    A carrier on the same terms as :class:`SessionHandle`: it holds seams and
    no behaviour, so ``turn.result()`` reaches an engine rather than calling a
    surface that should have owned shared behaviour.
    """

    turn: Turn[T]
    events: EventStream | None = None
    interrupt: Interrupt | None = None
    steer: Steer | None = None


class TurnToolBinding[T: BaseModel](
    BaseModel, frozen=True, arbitrary_types_allowed=True
):
    """Turn-local schema, store, and optional reflection gate."""

    output_type: type[T]
    store: SubmittedOutputStore
    gate: SubmissionGate[T] | None = None
