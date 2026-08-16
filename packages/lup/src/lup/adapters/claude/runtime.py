"""Claude SessionFactory composition with per-turn MCP tool rebinding."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mcp.server import Server
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, Field

from lup.mcp import LupMcpServerConfig, McpServerEntry
from lup.hooks import LupHooksConfig
from lup.policy.enforcement import SandboxPosture
from lup.runtime.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.runtime.contracts import (
    EventStream,
    ForkSession,
    Interrupt,
    TurnToolBinder,
)
from lup.runtime.errors import (
    DeltaStreamingDisabled,
    ProviderTurnError,
    QuotaExceededError,
    TurnError,
    TurnFailure,
    TurnInterruptedError,
)
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    BlockCompletedEvent,
    BlockDeltaEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionHandle,
    SessionId,
    SubmissionGateResolver,
    AnyTurnBlock,
    TurnIdentifiers,
    TurnId,
    TurnCompletedEvent,
    TurnEvent,
    TurnStartedEvent,
    TurnMessage,
    TurnToolBinding,
)
from lup.runtime.transcript import fold_blocks, fold_transcript
from lup.runtime.output import TurnSubmission, bound_submission
from lup.types import (
    EnvVars,
    JsonObject,
    JsonValue,
    SubagentSpec,
    Usage,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types


SESSION_THINKING_TOKENS = 128_000 - 1

type ClaudeSettingSource = Literal["user", "project", "local"]
"""One filesystem settings source the CLI may load for a session."""


class ClaudeSandboxConfig(BaseModel, frozen=True):
    """Claude SDK sandbox settings consumed by this factory."""

    enabled: bool = True
    auto_allow_bash_if_sandboxed: bool = True
    allow_unsandboxed_commands: bool = False
    excluded_commands: list[str] = Field(
        default=[],
        description=(
            "Command prefixes this session runs outside the boundary. A "
            "spawned session inherits none of the launching shell's settings "
            "files, so a requirement stated there reaches it only by being "
            "passed here too"
        ),
    )

    def posture(self) -> SandboxPosture:
        """These settings as the policy judging this session reads them.

        The same two values reach the CLI and the policy from this one
        object, so a session cannot be permitted more or less than whatever
        judges it believes. Read off a runtime constant instead, the two
        drifted the only way they can: the policy granted an escape the
        settings forbade, and the runtime dropped it without a word.

        The two halves are not equally firm, and only one of them settles
        its own question. ``allow_unsandboxed_commands`` decides the escape
        outright — off, the per-call argument is ignored. ``enabled`` only
        asks for a sandbox: where one cannot start, the CLI warns and runs
        the session unconfined, and this reports a boundary that is not
        there. The CLI settles that with a fail-if-unavailable setting, which
        this configuration cannot reach because the SDK's sandbox settings do
        not carry it; until they do, the placement is settled here and the
        confinement is asserted.
        """
        return SandboxPosture(
            active=self.enabled, escapable=self.allow_unsandboxed_commands
        )


type ClaudePermissionMode = Literal[
    "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"
]
"""Claude Code's own words for how much a session may do without asking."""


class ClaudeSessionConfig(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Immutable Claude-only provider configuration."""

    model: str | None = None
    system_prompt: str = ""
    coding_harness_preset: bool = True
    tools: list[str] | None = None
    allowed_tools: list[str] = []
    tool_servers: dict[str, McpServerEntry] = {}
    permission_mode: ClaudePermissionMode | None = "bypassPermissions"
    max_turns: int | None = None
    delta_streaming: bool = True
    """Whether partial-message deltas are streamed, which gates `live()`."""

    max_thinking_tokens: int | None = SESSION_THINKING_TOKENS
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    cwd: Path | None = None
    add_dirs: list[Path] = []
    plugin_dirs: list[Path] = []
    """Plugin directories this session loads, the way `--plugin-dir` does.

    A session given none of these resolves plugins through the project
    settings at `cwd` instead, and those name a marketplace by a key shared
    across every checkout that declares it — so a session opened in one
    worktree can be judged by a plugin generated from another's commit.
    Naming the directory is what makes a session load the tree it is in.
    """
    environment: EnvVars = {}
    sandbox: ClaudeSandboxConfig | None = None
    hooks: LupHooksConfig | None = None
    submission_gate_resolver: SubmissionGateResolver | None = None
    subagents: list[SubagentSpec] = []
    max_buffer_size: int | None = None
    stderr_tail_lines: int = Field(
        default=50,
        description=(
            "How many trailing CLI stderr lines are kept to explain a dead "
            "subprocess. Bounded because stderr is unbounded and only the "
            "end of it says why the process stopped."
        ),
    )
    setting_sources: list[ClaudeSettingSource] | None = None
    extra_args: dict[str, str | None] = {}  # lup: ignore[dict-str-payload]


type SubmissionBindingSource = Callable[[], TurnSubmission | None]


def attach_cli_stderr(error: Exception, stderr_lines: deque[str]) -> None:
    """Splice captured CLI stderr into the SDK's opaque process error.

    The SDK raises ``ProcessError`` carrying a fixed "Check stderr output for
    details" and never attaches the stderr it is pointing at, even when a
    stderr callback is set — so the exception names where the answer would be
    instead of carrying it, and every message built from it downstream
    inherits the same blank. Rewriting the message from the captured tail
    turns a blind ``exit code 1`` into the reason the process died, in place,
    so callers that only ever read ``str(error)`` need to know nothing about
    this. The ``exit code N`` token survives the rewrite, leaving exit-code
    matching reading what it read before.

    Anything that is not a ``ProcessError``, or a process that died saying
    nothing, is left exactly as it arrived.
    """
    from claude_agent_sdk import ProcessError

    if not isinstance(error, ProcessError):
        return

    tail = "\n".join(stderr_lines).strip()
    if not tail:
        return

    error.stderr = tail
    error.args = (
        f"Command failed with exit code {error.exit_code}\nError output: {tail}",
    )


ENVIRONMENTAL_SIGNATURES: tuple[str, ...] = (
    "oauth access token has been revoked",
    "failed to authenticate",
    "authentication_error",
    "invalid api key",
    "not logged in",
    "please run /login",
    "credit balance is too low",
    "session limit",
    "usage limit",
    "rate limit",
    "rate_limit_error",
    "overloaded_error",
    "api error: 401",
    "api error: 403",
    "api error: 429",
    "api error: 500",
    "api error: 502",
    "api error: 503",
    "api error: 529",
    "connection error",
    "connection reset",
    "temporary failure in name resolution",
)
"""Which CLI failures name the host rather than the work.

Claude Code reports an expired login, an exhausted allowance and a refused
upstream as ordinary process output rather than as typed exceptions, so
reading its words is the only place the distinction exists. Matched as
lowercase substrings against the whole message, since the CLI wraps the same
cause differently depending on where it surfaced.

Our judgement rather than the provider's vocabulary, so a caller replaces
it instead of forking this module. Over-matching costs a run that parks and
resumes; under-matching costs a concern recorded as having failed when its
credential died, which is the direction worth erring away from.
"""


def environmental_fault(
    message: str, signatures: tuple[str, ...] = ENVIRONMENTAL_SIGNATURES
) -> bool:
    """Whether this failure's own words name the host rather than the turn.

    Takes the message rather than the exception, because the message is what
    survives: several layers above the adapter re-wrap a raw exception into a
    fresh failure carrying `str(error)` and nothing else, so a caller reading
    a failure long after it was raised has the words and not the object.
    """
    return any(signature in message.casefold() for signature in signatures)


def quota_exhausted(
    spent: "claude_types.RateLimitInfo | None",
    blocks: list[AnyTurnBlock],
    identifiers: TurnIdentifiers,
) -> QuotaExceededError:
    """Build the typed exhaustion carrying the provider's own reset moment.

    A 429 with no accompanying rate-limit event says only that the allowance
    is gone, so the reset stays ``None`` and a waiter falls back to its own
    interval rather than inventing a time the provider never gave.
    """
    reset_at = (
        datetime.fromtimestamp(spent.resets_at, tz=UTC)
        if spent is not None and spent.resets_at is not None
        else None
    )
    until = f" until {reset_at.isoformat()}" if reset_at is not None else ""
    return QuotaExceededError(
        TurnFailure(
            message=f"Claude account allowance exhausted{until}",
            blocks=blocks,
            identifiers=identifiers,
            environmental=True,
        ),
        reset_at=reset_at,
        quota_type=spent.rate_limit_type if spent is not None else None,
    )


HUMAN_CLEARED_SIGNATURES: tuple[str, ...] = (
    "oauth access token has been revoked",
    "failed to authenticate",
    "authentication_error",
    "invalid api key",
    "not logged in",
    "please run /login",
    "credit balance is too low",
    "api error: 401",
    "api error: 403",
)
"""Which host faults stay broken until a person does something.

A dead credential and an empty balance do not come back on their own, so
waiting one out is waiting forever. Every other refusal here — an exhausted
allowance, a rate limit, an overloaded or unreachable upstream — clears with
nobody doing anything, and those are worth waiting for rather than handing
back to whoever has to notice.

Our judgement rather than the provider's vocabulary, so a caller replaces it
instead of forking this module. Over-matching costs a run that stops when it
could have waited; under-matching costs one that sleeps through a credential
nobody is going to renew, so this errs toward naming what a person must fix.
"""


def needs_a_person(
    message: str, signatures: tuple[str, ...] = HUMAN_CLEARED_SIGNATURES
) -> bool:
    """Whether this host fault stays broken until somebody acts on it."""
    return any(signature in message.casefold() for signature in signatures)


ROTATION_AMBIGUOUS_SIGNATURES: tuple[str, ...] = (
    "oauth access token has been revoked",
    "failed to authenticate",
    "authentication_error",
    "not logged in",
    "please run /login",
    "api error: 401",
)
"""Which of those faults a sibling's token refresh produces identically.

Concurrent sessions share one credential file, because the alternative is
worse: a private copy each would mean the first refresh rotates the token and
strands every other copy for good. Sharing costs a race instead. A refresh
rotates, so the moment one session renews, every sibling still holding the
previous token is denied — in the words the provider also uses for a
credential nobody is going to renew.

The two are indistinguishable in the message and opposite in what they need,
so the run asks the only question that separates them: a session opened
afresh reads the rotated file and succeeds, where a dead credential fails
again. One probe is the whole difference, and it costs a delay to be wrong.

An empty balance and a rejected key are absent deliberately. Neither is
reachable by rotation, so probing one only postpones handing it back.
"""


def may_be_a_rotation(
    message: str, signatures: tuple[str, ...] = ROTATION_AMBIGUOUS_SIGNATURES
) -> bool:
    """Whether a fresh session could find this fault already cleared."""
    return any(signature in message.casefold() for signature in signatures)


def turn_error(interrupt: "ClaudeInterrupt") -> type[TurnError]:
    """Classify a failed turn the way Codex's terminal status does."""
    return TurnInterruptedError if interrupt.requested else ProviderTurnError


class ClaudeConversationState:
    """Adapter-private reconnect/resume state for one Lup session."""

    def __init__(self, opener: "ClaudeSessionOpener", resume: SessionId | None) -> None:
        self.opener = opener
        self.config = opener.config
        self.resume = resume.value if resume is not None else None
        self.session_id = self.resume or str(uuid4())
        self.client: claude.ClaudeSDKClient | None = None
        self.submission: TurnSubmission | None = None
        self.schema_digest: str | None = None
        self.completion: asyncio.Task[CompletedTurn] | None = None
        self.stderr_lines: deque[str] = deque(maxlen=self.config.stderr_tail_lines)

    def current_submission(self) -> TurnSubmission | None:
        """Resolve the submission a live connection's tool should serve."""
        return self.submission

    async def settle_reader(self) -> None:
        """Unwind an unfinished turn's read before its transport goes away.

        Leaving a session interrupts the active turn without awaiting it, so
        the reader can still be suspended inside `receive_response()` when the
        transport closes that generator underneath it. Cancelling is what
        bounds the wait: awaiting the turn itself would hang teardown on any
        turn that never terminates.
        """
        completion = self.completion
        self.completion = None
        if completion is None or completion.done():
            return
        completion.cancel()
        await asyncio.wait([completion])

    async def disconnect(self) -> None:
        if self.client is None:
            return
        await self.settle_reader()
        try:
            await self.client.disconnect()
        finally:
            self.client = None

    async def connect(self) -> "claude.ClaudeSDKClient":
        if self.client is not None:
            return self.client
        # The runtime assigns the id and this state reads it off the first
        # result, mirroring `CodexTurnChannel.ensure_thread`. Dictating one
        # instead let the two adapters disagree about who owns a session's
        # identity, and only one of them can be right about a conversation the
        # provider is the one persisting.
        options = self.opener.build_options(
            binding=self.current_submission,
            resume=self.resume,
            session_id=None,
        )
        # Connecting is where a refused resume surfaces, and it is as much a
        # failed turn as one that breaks midway — so it leaves through the
        # portable error the rest of the runtime raises. Escaping as the SDK's
        # own exception let it past every caller that handles a turn failing,
        # which is how a resume the provider had lost ended whole runs.
        try:
            self.client = await self.connected(options)
        except Exception as error:
            if self.resume is None:
                raise ProviderTurnError(
                    TurnFailure(
                        message=str(error),
                        environmental=environmental_fault(str(error)),
                    )
                ) from error
            # The provider no longer holds what this state was resuming. A
            # turn that cannot reach its history still beats one that cannot
            # happen, so the conversation is forgotten rather than the run.
            logger.warning(
                "Claude refused to resume session %s (%s); continuing on a new one",
                self.resume,
                error,
            )
            self.resume = None
            try:
                self.client = await self.connected(
                    self.opener.build_options(
                        binding=self.current_submission, resume=None, session_id=None
                    )
                )
            except Exception as fresh_error:
                raise ProviderTurnError(
                    TurnFailure(
                        message=str(fresh_error),
                        environmental=environmental_fault(str(fresh_error)),
                    )
                ) from fresh_error
        return self.client

    async def connected(
        self, options: "claude.ClaudeAgentOptions"
    ) -> "claude.ClaudeSDKClient":
        """One connected client for these options, or the failure that stopped it."""
        import claude_agent_sdk as claude

        options.stderr = self.stderr_lines.append
        client = claude.ClaudeSDKClient(options=options)
        try:
            await client.connect()
        except Exception as error:
            attach_cli_stderr(error, self.stderr_lines)
            raise
        return client

    async def start_turn(self, text: str) -> AcceptedTurn:
        client = await self.connect()
        await client.query(text, session_id=self.session_id)
        identifiers = TurnIdentifiers(
            session=SessionId(value=self.session_id),
            turn=TurnId(value=uuid4().hex),
        )
        events: asyncio.Queue[LiveTurnEvent | None] = asyncio.Queue()
        events.put_nowait(TurnStartedEvent(identifiers=identifiers))
        interrupt = ClaudeInterrupt(self)

        async def complete() -> CompletedTurn:
            from claude_agent_sdk import types as claude_types

            nonlocal identifiers
            durable: list[TurnEvent] = []  # lup: ignore[empty-collection]
            result: claude_types.ResultMessage | None = None
            exhausted: claude_types.RateLimitInfo | None = None
            started = perf_counter()

            def record(message: TurnMessage) -> None:
                """Emit one message and its blocks, so both views agree."""
                for block in message.blocks:
                    completed = BlockCompletedEvent(
                        identifiers=identifiers, block=block
                    )
                    durable.append(completed)
                    events.put_nowait(completed)
                whole = MessageCompletedEvent(identifiers=identifiers, message=message)
                durable.append(whole)
                events.put_nowait(whole)

            try:
                async for message in client.receive_response():
                    match message:
                        case claude_types.AssistantMessage(
                            content=content,
                            parent_tool_use_id=delegated_under,
                            model=message_model,
                            message_id=message_id,
                        ):
                            record(
                                TurnMessage(
                                    role="assistant",
                                    blocks=[
                                        convert_claude_block(block) for block in content
                                    ],
                                    parent_tool_call_id=delegated_under,
                                    model=message_model,
                                    message_id=message_id,
                                )
                            )
                        case claude_types.UserMessage(
                            content=content,
                            parent_tool_use_id=delegated_under,
                        ) if isinstance(content, list):
                            record(
                                TurnMessage(
                                    role="tool",
                                    blocks=[
                                        convert_claude_block(block) for block in content
                                    ],
                                    parent_tool_call_id=delegated_under,
                                )
                            )
                        case claude_types.UserMessage(content=str(text)):
                            from lup.runtime.models import TurnTextBlock

                            record(
                                TurnMessage(
                                    role="user", blocks=[TurnTextBlock(text=text)]
                                )
                            )
                        case claude_types.ResultMessage() as terminal:
                            result = terminal
                        # The provider says an allowance is spent, and says
                        # when it returns. Read as data rather than matched
                        # out of the error text, that reset time is what a
                        # waiter can sleep against exactly.
                        case claude_types.RateLimitEvent(
                            rate_limit_info=claude_types.RateLimitInfo(
                                status="rejected"
                            ) as spent
                        ):
                            exhausted = spent
                        # The CLI persists the transcript under its own id,
                        # not the channel id this side minted — adopting it is
                        # what lets a later resume name a conversation that
                        # actually exists on disk.
                        case claude_types.SystemMessage(
                            subtype="init", data={"session_id": str(adopted)}
                        ) if adopted != self.session_id:
                            self.session_id = adopted
                            self.resume = adopted
                            identifiers = identifiers.model_copy(
                                update={"session": SessionId(value=adopted)}
                            )
                        case claude_types.StreamEvent(event=event):
                            match event:
                                case {
                                    "type": "content_block_delta",
                                    "delta": {"text": str(delta)},
                                } | {
                                    "type": "content_block_delta",
                                    "delta": {"thinking": str(delta)},
                                }:
                                    events.put_nowait(
                                        BlockDeltaEvent(
                                            identifiers=identifiers,
                                            delta=delta,
                                        )
                                    )
            except Exception as error:
                attach_cli_stderr(error, self.stderr_lines)
                raise turn_error(interrupt)(
                    TurnFailure(
                        message=str(error),
                        blocks=fold_blocks(durable),
                        duration=timedelta(seconds=perf_counter() - started),
                        identifiers=identifiers,
                        environmental=environmental_fault(str(error)),
                    )
                ) from error

            if exhausted is not None or (
                result is not None and result.api_error_status == 429
            ):
                raise quota_exhausted(exhausted, fold_blocks(durable), identifiers)
            if result is None:
                raise ProviderTurnError(
                    TurnFailure(
                        message="Claude completed without a terminal result",
                        blocks=fold_blocks(durable),
                        identifiers=identifiers,
                    )
                )
            if result.session_id is not None:
                self.session_id = result.session_id
                self.resume = result.session_id
            messages = fold_transcript(durable)
            blocks = fold_blocks(durable)
            usage = claude_usage(result.usage, total_cost_usd=result.total_cost_usd)
            duration = timedelta(milliseconds=result.duration_ms or 0)
            if result.is_error:
                raise turn_error(interrupt)(
                    TurnFailure(
                        message=str(result.result or "Claude turn failed"),
                        blocks=blocks,
                        usage=usage,
                        duration=duration,
                        identifiers=identifiers,
                    )
                )
            return CompletedTurn(
                messages=messages,
                blocks=blocks,
                usage=usage,
                duration=duration,
                identifiers=identifiers,
            )

        async def complete_with_events() -> CompletedTurn:
            try:
                return await complete()
            finally:
                events.put_nowait(TurnCompletedEvent(identifiers=identifiers))
                events.put_nowait(None)

        completion = asyncio.create_task(complete_with_events())
        self.completion = completion

        def observe_completion(task: asyncio.Task[CompletedTurn]) -> None:
            if not task.cancelled():
                task.exception()

        completion.add_done_callback(observe_completion)

        async def await_completion() -> CompletedTurn:
            return await completion

        return AcceptedTurn(
            identifiers=identifiers,
            complete=await_completion,
            events=ClaudeLiveEventStream(events, self.config.delta_streaming),
            interrupt=interrupt,
        )


class ClaudeLiveEventStream(EventStream):
    """One ordered queue, viewed either with in-flight deltas or without.

    The durable view filters the same sequence rather than reading a second
    one, so the two can never report different histories.
    """

    def __init__(
        self,
        events: asyncio.Queue[LiveTurnEvent | None],
        delta_streaming: bool = False,
    ) -> None:
        self.queue = events
        self.consumed = False
        self.delta_streaming = delta_streaming

    async def iterate(self) -> AsyncIterator[LiveTurnEvent]:
        if self.consumed:
            raise RuntimeError("live event stream can only be consumed once")
        self.consumed = True
        while (event := await self.queue.get()) is not None:
            yield event

    async def durable(self) -> AsyncIterator[TurnEvent]:
        async for event in self.iterate():
            if (durable := event.durable) is not None:
                yield durable

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        if not self.delta_streaming:
            raise DeltaStreamingDisabled(
                "this session was built without partial message streaming"
            )
        return self.iterate()


class ClaudeTurnToolBinder(TurnToolBinder):
    """Refresh handler state in place and reconnect only to change the schema.

    A connection advertises its submission schema once, so a turn that asks for
    a different one has to reconnect. A turn that asks for the same one does
    not, and reconnecting anyway would spend the conversation to install a tool
    identical to the one already there — which is what every worker turn does,
    against a provider that no longer persists a transcript to resume.
    """

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state

    async def bind[T: BaseModel](self, binding: TurnToolBinding[T] | None) -> None:
        if binding is None and self.state.submission is None:
            return
        submission = bound_submission(binding) if binding is not None else None
        digest = submission.digest if submission is not None else None
        if digest != self.state.schema_digest:
            await self.state.disconnect()
        self.state.schema_digest = digest
        self.state.submission = submission


class ClaudeInterrupt(Interrupt):
    """Interrupt the currently connected Claude turn, and remember asking.

    The SDK ends an interrupted turn the way it ends a failed one, so what
    separates them is only that someone asked. Recording the request is what
    lets the turn raise as interrupted rather than as a provider failure the
    recovery wrapper would dutifully retry.
    """

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state
        self.requested = False

    async def interrupt(self) -> None:
        self.requested = True
        if self.state.client is not None:
            await self.state.client.interrupt()


class ClaudeFork(ForkSession):
    """Fork the latest persisted Claude transcript into a new typed session."""

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state

    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager[SessionHandle]:
        return self.open_fork(at)

    @asynccontextmanager
    async def open_fork(self, at: TurnId | None) -> AsyncGenerator[SessionHandle]:
        if at is not None:
            raise ValueError("Claude transcript forking supports the latest turn only")
        import claude_agent_sdk as claude

        directory = (
            str(self.state.config.cwd) if self.state.config.cwd is not None else None
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = await asyncio.get_running_loop().run_in_executor(
                executor,
                claude.fork_session,
                self.state.session_id,
                directory,
            )
        opener = self.state.opener
        async with opener.open_session(SessionId(value=result.session_id)) as handle:
            yield handle


class ClaudeSessionOpener:
    """Open independently configured reconnecting Claude sessions."""

    def __init__(self, config: ClaudeSessionConfig) -> None:
        self.config = config

    def create_state(self, resume: SessionId | None) -> ClaudeConversationState:
        """Construct the reconnect state backing one opened session."""
        return ClaudeConversationState(self, resume)

    def build_options(
        self,
        *,
        binding: SubmissionBindingSource,
        resume: str | None,
        session_id: str | None,
    ) -> "claude.ClaudeAgentOptions":
        """Build the SDK options for one connection of this factory's sessions."""
        return build_claude_options(
            self.config, binding=binding, resume=resume, session_id=session_id
        )

    @asynccontextmanager
    async def open_session(
        self, resume: SessionId | None = None
    ) -> AsyncGenerator[SessionHandle]:
        state = self.create_state(resume)
        session = ComposedSession(
            starter=state.start_turn,
            binder=ClaudeTurnToolBinder(state),
            gate_resolver=self.config.submission_gate_resolver,
            submission_tool=SUBMISSION_TOOL,
        )
        # lup: defer: A resolver run that parks can end on `an error occurred
        # during closing of asynchronous generator <ClaudeSessionOpener.
        # open_session>: RuntimeError: aclose(): asynchronous generator is
        # already running`, after the park output is complete and with exit
        # code 0 — so successful work reads as failed. Two candidates are
        # already refuted: `settle_reader` below was in the build that
        # reported it, and a concurrent double close of the actor's exit
        # stack cannot collide, because `AsyncExitStack.__aexit__` pops each
        # callback before awaiting it. What is left is that `asyncio.run`
        # cancels leftover tasks before finalizing async generators, so a
        # task still suspended inside this `finally` when the run returns
        # leaves the generator running for the finalizer's own `aclose` to
        # trip over — which means finding who closes a session without
        # awaiting it to completion. Needs a live parking run to confirm;
        # do not fix it from the shape alone.
        try:
            yield SessionHandle(session=session, fork=ClaudeFork(state))
        finally:
            try:
                await session.abort_active()
            finally:
                await state.disconnect()


# lup: ignore[model-free-function] — composition root over the session config
def create_claude_session_factory(
    config: ClaudeSessionConfig,
) -> SessionFactory:
    """Create the named Claude runtime composition root."""
    return SessionFactory(ClaudeSessionOpener(config).open_session)


# The fully qualified name of the turn-bound submission tool as Claude Code
# sees it. Compositions that install their own tool-allowlist hooks must
# include it, or the hook denies the very tool the turn requires.
# lup: ignore[constant-declaration] — the qualified name Claude Code composes
SUBMISSION_TOOL = "mcp__lup-output__submit_output"


def build_submission_server(
    current: SubmissionBindingSource,
) -> LupMcpServerConfig:
    """Build an exact-schema MCP server reading the binding the turn installed.

    The schema is fixed for a connection's lifetime because changing it
    reconnects, but a same-schema turn refreshes store and gate in place. So
    the handler resolves the binding when it runs rather than closing over the
    one that happened to be installed when the connection opened, which would
    write every later turn's output into the first turn's store.
    """
    server = Server("lup-output", version="1")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        submission = current()
        if submission is None:
            return []
        return [
            Tool(
                name="submit_output",
                description="Submit the final validated result for this turn.",
                inputSchema=submission.schema,
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: JsonObject) -> CallToolResult:
        if name != "submit_output":
            raise ValueError(f"unknown output tool {name!r}")
        submission = current()
        if submission is None:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="No matching turn output binding is active.",
                    )
                ],
                isError=True,
            )
        response = await submission.submit(arguments)
        return CallToolResult(
            content=[TextContent(type="text", text=response.message)],
            isError=not response.accepted,
        )

    return LupMcpServerConfig(
        name="lup-output",
        server=server,
        tool_names=["submit_output"],
    )


# lup: ignore[model-free-function] — renders the declaration into the SDK's shape
def build_claude_options(
    config: ClaudeSessionConfig,
    *,
    binding: SubmissionBindingSource,
    resume: str | None,
    session_id: str | None,
) -> "claude.ClaudeAgentOptions":
    """Build SDK options lazily and never enable native structured output."""
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types
    from lup.adapters.claude.hooks import lup_hooks_to_claude

    servers = dict(config.tool_servers)
    allowed = list(config.allowed_tools)
    if binding() is not None:
        servers["lup-output"] = build_submission_server(binding)
        allowed.append(SUBMISSION_TOOL)

    def native_server(server: McpServerEntry) -> "claude_types.McpServerConfig":
        """Project one entry into the server config this SDK's options take.

        The projection belongs here because it is this provider's spelling: a
        server we host becomes an SDK config, while an external one already is
        the SDK's transport shape and passes through. Asking the neutral entry
        to convert itself would move that spelling into library code, beside a
        second adapter that projects the same entry into an unrelated
        subprocess shape.
        """
        match server:
            case LupMcpServerConfig():  # lup: ignore[own-model-dispatch] — seam
                return claude_types.McpSdkServerConfig(
                    type="sdk", name=server.name, instance=server.server
                )
            case _:
                return server

    sandbox = (
        claude_types.SandboxSettings(
            enabled=config.sandbox.enabled,
            autoAllowBashIfSandboxed=config.sandbox.auto_allow_bash_if_sandboxed,
            allowUnsandboxedCommands=config.sandbox.allow_unsandboxed_commands,
            excludedCommands=list(config.sandbox.excluded_commands),
        )
        if config.sandbox is not None
        else None
    )
    system_prompt: str | claude_types.SystemPromptPreset | None = (
        {
            "type": "preset",
            "preset": "claude_code",
            "append": config.system_prompt,
        }
        if config.coding_harness_preset
        else config.system_prompt or None
    )
    return claude.ClaudeAgentOptions(
        model=config.model,
        system_prompt=system_prompt,
        tools=config.tools,
        allowed_tools=list(dict.fromkeys(allowed)),
        mcp_servers={name: native_server(server) for name, server in servers.items()},
        agents={
            spec.name: claude_types.AgentDefinition(
                description=spec.description,
                prompt=spec.prompt,
                tools=spec.tools,
                model=spec.model,
            )
            for spec in config.subagents
        }
        or None,
        permission_mode=config.permission_mode,
        max_turns=config.max_turns,
        max_thinking_tokens=config.max_thinking_tokens,
        effort=config.effort,
        cwd=config.cwd,
        add_dirs=[str(path) for path in config.add_dirs],
        plugins=[
            claude_types.SdkPluginConfig(type="local", path=str(path))
            for path in config.plugin_dirs
        ],
        env=config.environment,
        sandbox=sandbox,
        hooks=(
            lup_hooks_to_claude(config.hooks)
            if config.hooks is not None and config.hooks.by_event()
            else None
        ),
        include_partial_messages=config.delta_streaming,
        resume=resume,
        session_id=session_id,
        output_format=None,
        max_buffer_size=config.max_buffer_size,
        setting_sources=config.setting_sources,
        extra_args=dict(config.extra_args),
    )


def convert_claude_block(block: "claude.ContentBlock") -> AnyTurnBlock:
    """Convert one SDK block directly into the portable runtime vocabulary."""
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    match block:
        case claude.TextBlock(text=text):
            from lup.runtime.models import TurnTextBlock

            return TurnTextBlock(text=text)
        case claude.ThinkingBlock(thinking=thinking, signature=signature):
            from lup.runtime.models import TurnThinkingBlock

            return TurnThinkingBlock(
                thinking=thinking or "", redacted=not thinking and bool(signature)
            )
        case claude.ToolUseBlock(id=identifier, name=name, input=input_data):
            from lup.runtime.models import TurnToolCallBlock

            return TurnToolCallBlock(
                id=identifier, name=name, arguments=input_data or {}
            )
        case claude.ToolResultBlock(tool_use_id=identifier, content=content):
            from lup.runtime.models import TurnToolResultBlock

            rendered = (
                content
                if isinstance(content, str)
                else json.dumps(content, default=str)
            )
            return TurnToolResultBlock(
                tool_call_id=identifier,
                content=rendered or "",
            )
        case claude_types.ServerToolUseBlock(
            id=identifier, name=name, input=input_data
        ):
            from lup.runtime.models import TurnToolCallBlock

            return TurnToolCallBlock(
                id=identifier, name=name, arguments=input_data or {}
            )
        case claude_types.ServerToolResultBlock(
            tool_use_id=identifier, content=content
        ):
            from lup.runtime.models import TurnToolResultBlock

            return TurnToolResultBlock(
                tool_call_id=identifier,
                content=content if isinstance(content, str) else str(content),
            )
        case _:
            from lup.runtime.models import TurnTextBlock

            return TurnTextBlock(text=str(block))


def claude_usage(
    raw: Mapping[str, JsonValue] | None,
    *,
    total_cost_usd: float | None = None,
) -> Usage:
    """Normalize only the portable count fields from Claude's open payload."""
    if raw is None:
        return Usage(cost_usd=total_cost_usd)

    def count(name: str) -> int:
        value = raw.get(name)  # lup: ignore[dict-get] -- vendor payload
        return value if isinstance(value, int) else 0

    return Usage(
        cost_usd=total_cost_usd,
        input_tokens=count("input_tokens")
        + count("cache_read_input_tokens")
        + count("cache_creation_input_tokens"),
        output_tokens=count("output_tokens"),
        cache_read_input_tokens=count("cache_read_input_tokens"),
        cache_creation_input_tokens=count("cache_creation_input_tokens"),
    )
