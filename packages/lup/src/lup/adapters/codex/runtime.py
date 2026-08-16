"""Codex app-server SessionFactory with live optional turn capabilities."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from lup.adapters.codex.app_server import CodexAppServer, RpcMessage, RpcNotification
from lup.adapters.codex.hooks import (
    APPROVAL_METHODS,
    CodexApprovalResponder,
)
from lup.hooks import LupHooksConfig
from lup.runtime.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.runtime.contracts import (
    EventStream,
    ForkSession,
    Interrupt,
    Steer,
    TurnToolBinder,
)
from lup.runtime.errors import ProviderTurnError, TurnFailure, TurnInterruptedError
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
    TurnCompletedEvent,
    TurnEvent,
    TurnStartedEvent,
    TurnIdentifiers,
    TurnId,
    TurnInput,
    TurnMessage,
    TurnToolBinding,
)
from lup.runtime.output import TurnSubmission, bound_submission
from lup.runtime.transcript import fold_transcript
from lup.types import EnvVars, JsonObject, JsonValue, Usage


class CodexSessionConfig(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Immutable Codex-only app-server configuration."""

    model: str | None = None
    developer_instructions: str = ""
    cwd: Path
    executable: Path = Path("codex")
    named_profile: str | None = None
    model_provider: str | None = None
    provider_config: JsonObject | None = None
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    # The app-server's own wire spellings, passed through by thread_parameters.
    approval_policy: Literal["untrusted", "on-request", "granular", "never"] | None = (
        None
    )
    hooks: LupHooksConfig | None = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    environment: EnvVars = {}
    submission_gate_resolver: SubmissionGateResolver | None = None
    mcp_servers: dict[str, "CodexMcpServerConfig"] = {}
    writable_roots: list[Path] = []

    @model_validator(mode="after")
    def reject_unanswerable_approvals(self) -> "CodexSessionConfig":
        """Refuse a thread that would ask questions this session cannot answer.

        An approval policy that asks makes the app-server send approval
        requests back here, and only declared hooks answer them. Without
        those the transport refuses every request as unhandled, which stalls
        the turn on its first command — so the combination is rejected at
        construction instead of at the first act.
        """
        if self.approval_policy not in {None, "never"} and self.hooks is None:
            raise ValueError(
                f"approval_policy {self.approval_policy!r} makes the app-server "
                "ask this session for decisions; supply hooks to answer them, "
                "or use 'never'"
            )
        return self


class CodexMcpServerConfig(BaseModel, frozen=True):
    """One project tool group served to Codex over an explicit subprocess."""

    command: str
    args: list[str] = []
    env: EnvVars = {}


class CodexThreadRef(BaseModel, frozen=True):
    id: str


class CodexTurnRef(BaseModel, frozen=True):
    id: str
    status: str = "inProgress"
    duration_ms: int | None = Field(default=None, alias="durationMs")


class CodexThreadResponse(BaseModel, frozen=True):
    thread: CodexThreadRef


class CodexTurnResponse(BaseModel, frozen=True):
    turn: CodexTurnRef


class DynamicToolCall(BaseModel, frozen=True):
    thread_id: str = Field(alias="threadId")
    turn_id: str = Field(alias="turnId")
    call_id: str = Field(alias="callId")
    tool: str
    arguments: JsonValue


class McpElicitationRequest(BaseModel, frozen=True):
    """One approval elicitation for an MCP server tool call.

    Codex treats session-scoped MCP servers as untrusted and elicits an
    approval (``mcpServer/elicitation/request``, with
    ``_meta.codex_approval_kind = "mcp_tool_call"``) before every call.
    """

    thread_id: str = Field(alias="threadId")
    server_name: str = Field(alias="serverName")


class TokenUsageBreakdown(BaseModel, frozen=True):
    input_tokens: int = Field(alias="inputTokens")
    output_tokens: int = Field(alias="outputTokens")
    cached_input_tokens: int = Field(alias="cachedInputTokens")


class CodexSchemaRebindingError(RuntimeError):
    """The current app-server cannot change thread-scoped dynamic tools safely."""


def notification_turn_id(notification: RpcNotification) -> str | None:
    """Extract the native turn identity without mutating channel ownership."""
    match notification.params:
        case {"turnId": str(turn_id)}:
            return turn_id
        case {"turn": {"id": str(turn_id)}}:
            return turn_id
        case _:
            return None


class CodexTurnChannel:
    """Route one turn's notifications into live events and completed replay."""

    notifications: Sequence[str] = (
        "turn/started",
        "item/agentMessage/delta",
        "item/completed",
        "thread/tokenUsage/updated",
        "turn/completed",
    )
    """Every notification method :meth:`decode` answers.

    Those arms narrow vendor method strings, which no union of ours enumerates,
    so the roster is declared here beside them and gates the match rather than
    describing it. A method that gains an arm without gaining an entry never
    reaches it, and the suite naming each shape fails instead of a renamed
    notification passing as one this turn had no interest in.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.turn_id: str | None = None
        self.events: asyncio.Queue[LiveTurnEvent | None] = asyncio.Queue()
        self.completed: asyncio.Future[CompletedTurn] = (
            asyncio.get_running_loop().create_future()
        )
        self.durable: list[TurnEvent] = []
        self.blocks: list[AnyTurnBlock] = []
        self.usage = Usage()
        self.started = perf_counter()

    def identifiers(self) -> TurnIdentifiers:
        if self.turn_id is None:
            raise RuntimeError("turn notification arrived without a turn identity")
        return TurnIdentifiers(
            session=SessionId(value=self.session_id),
            turn=TurnId(value=self.turn_id),
        )

    def emit(self, event: TurnEvent) -> None:
        """Record one durable event and publish it, so both views agree."""
        self.durable.append(event)
        self.events.put_nowait(event)

    def feed(self, notification: RpcNotification) -> None:
        try:
            self.decode(notification)
        except Exception as error:
            self.fail(error)

    def fail(self, error: Exception) -> None:
        """Fail this turn with all live evidence accumulated so far."""
        if isinstance(error, ProviderTurnError | TurnInterruptedError):
            failure = error
        else:
            identifiers = self.identifiers() if self.turn_id is not None else None
            failure = ProviderTurnError(
                TurnFailure(
                    message=str(error),
                    blocks=self.blocks,
                    usage=self.usage,
                    duration=timedelta(seconds=perf_counter() - self.started),
                    identifiers=identifiers,
                )
            )
        if not self.completed.done():
            self.completed.set_exception(failure)
        self.events.put_nowait(None)

    def decode(self, notification: RpcNotification) -> None:
        candidate = notification_turn_id(notification)
        if candidate is not None:
            if self.turn_id is not None and candidate != self.turn_id:
                return
            self.turn_id = candidate
        if notification.method not in self.notifications:
            return
        match notification.method, notification.params:
            case "turn/started", {"turn": {"id": str()}}:
                self.events.put_nowait(TurnStartedEvent(identifiers=self.identifiers()))
            case "item/agentMessage/delta", {
                "turnId": str(),
                "delta": str(delta),
            }:
                self.events.put_nowait(
                    BlockDeltaEvent(
                        identifiers=self.identifiers(),
                        delta=delta,
                    )
                )
            case "item/completed", {
                "turnId": str(),
                "item": item,
            } if isinstance(item, dict):
                completed = decode_completed_item(item)
                for block in completed:
                    self.blocks.append(block)
                    self.emit(
                        BlockCompletedEvent(identifiers=self.identifiers(), block=block)
                    )
                if completed:
                    self.emit(
                        MessageCompletedEvent(
                            identifiers=self.identifiers(),
                            message=TurnMessage(
                                role=message_role(item), blocks=completed
                            ),
                        )
                    )
            case "thread/tokenUsage/updated", {
                "turnId": str(),
                "tokenUsage": {"last": usage},
            } if isinstance(usage, dict):
                self.usage = decode_usage(usage)
            case "turn/completed", {
                "turn": {"id": str(), "status": str(status)} as turn
            }:
                duration_ms = turn.get("durationMs")  # lup: ignore[dict-get]
                duration = timedelta(
                    milliseconds=duration_ms
                    if isinstance(duration_ms, int)
                    else (perf_counter() - self.started) * 1000
                )
                if status == "completed":
                    messages = fold_transcript(self.durable)
                    if not self.completed.done():
                        self.completed.set_result(
                            CompletedTurn(
                                messages=messages,
                                blocks=self.blocks,
                                usage=self.usage,
                                duration=duration,
                            )
                        )
                elif not self.completed.done():
                    native_error = turn.get("error")  # lup: ignore[dict-get]
                    message = (
                        native_error.get("message")  # lup: ignore[dict-get]
                        if isinstance(native_error, dict)
                        else None
                    )
                    failure = TurnFailure(
                        message=(
                            message
                            if isinstance(message, str) and message
                            else f"Codex turn ended with status {status}"
                        ),
                        blocks=self.blocks,
                        usage=self.usage,
                        duration=duration,
                        identifiers=self.identifiers(),
                    )
                    error = (
                        TurnInterruptedError(failure)
                        if status.lower() in {"interrupted", "cancelled", "canceled"}
                        else ProviderTurnError(failure)
                    )
                    self.completed.set_exception(error)
                self.events.put_nowait(
                    TurnCompletedEvent(identifiers=self.identifiers())
                )
                self.events.put_nowait(None)
            case _:
                return


class CodexLiveEventStream(EventStream):
    """One ordered channel, viewed either with in-flight deltas or without.

    The durable view filters the same sequence rather than reading a second
    one, so the two can never report different histories.
    """

    def __init__(self, channel: CodexTurnChannel) -> None:
        self.channel = channel
        self.consumed = False

    async def iterate(self) -> AsyncIterator[LiveTurnEvent]:
        if self.consumed:
            raise RuntimeError("live event stream can only be consumed once")
        self.consumed = True
        while (event := await self.channel.events.get()) is not None:
            yield event

    async def durable(self) -> AsyncIterator[TurnEvent]:
        async for event in self.iterate():
            if (durable := event.durable) is not None:
                yield durable

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        return self.iterate()


# lup: ignore[constant-declaration] — what the app-server advertises the tool as
SUBMISSION_TOOL = "submit_output"
"""What the app-server advertises this turn's dynamic submission tool as."""


class CodexConversationState:
    """One app-server connection, thread, and current turn binding."""

    def __init__(
        self,
        config: CodexSessionConfig,
        server: CodexAppServer,
        resume: SessionId | None,
    ) -> None:
        self.config = config
        self.server = server
        self.resume = resume
        self.thread_id: str | None = None
        self.submission: TurnSubmission | None = None
        self.schema_digest: str | None = None
        self.channel: CodexTurnChannel | None = None
        self.server.server_request_handler = self.handle_server_request
        self.server.notification_handler = self.handle_notification
        self.server.disconnect_handler = self.handle_disconnect

    async def ensure_thread(self) -> str:
        if self.thread_id is not None:
            return self.thread_id
        if self.resume is not None:
            # Codex persists dynamic tools in the thread's rollout metadata and
            # restores them on resume when none are supplied, so a resumed
            # thread keeps the submission tool by saying nothing about it.
            # Refusing to resume with a binding at all was self-imposed: the
            # digest carried in from the persisted record is what still catches
            # a genuine schema change, in `bind` rather than here.
            params = self.thread_parameters()
            params["threadId"] = self.resume.value
            result = await self.server.request("thread/resume", params)
        else:
            params = self.thread_parameters()
            if self.submission is not None:
                params["dynamicTools"] = [dynamic_tool(self.submission)]
            result = await self.server.request("thread/start", params)
        response = CodexThreadResponse.model_validate(result)
        self.thread_id = response.thread.id
        return self.thread_id

    def thread_parameters(self) -> JsonObject:
        """Preserve configured thread behavior for new and resumed threads."""
        params: JsonObject = {
            "cwd": str(self.config.cwd),
            "developerInstructions": self.config.developer_instructions,
        }
        if self.config.model is not None:
            params["model"] = self.config.model
        if self.config.model_provider is not None:
            params["modelProvider"] = self.config.model_provider
        configuration = dict(self.config.provider_config or {})
        if self.config.mcp_servers:
            configuration["mcp_servers"] = {
                name: server.model_dump(mode="json")
                for name, server in self.config.mcp_servers.items()
            }
        if self.config.writable_roots:
            configuration["sandbox_workspace_write"] = {
                "writable_roots": [str(path) for path in self.config.writable_roots]
            }
        if configuration:
            params["config"] = configuration
        if self.config.sandbox is not None:
            params["sandbox"] = self.config.sandbox
        if self.config.approval_policy is not None:
            params["approvalPolicy"] = self.config.approval_policy
        return params

    async def start_turn(self, text: str) -> AcceptedTurn:
        thread_id = await self.ensure_thread()
        channel = CodexTurnChannel(thread_id)
        self.channel = channel
        params: JsonObject = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if self.config.effort is not None:
            params["effort"] = self.config.effort
        result = await self.server.request("turn/start", params)
        response = CodexTurnResponse.model_validate(result)
        channel.turn_id = response.turn.id
        identifiers = TurnIdentifiers(
            session=SessionId(value=thread_id),
            turn=TurnId(value=response.turn.id),
        )

        async def complete() -> CompletedTurn:
            return await channel.completed

        return AcceptedTurn(
            identifiers=identifiers,
            complete=complete,
            events=CodexLiveEventStream(channel),
            interrupt=CodexInterrupt(self, response.turn.id),
            steer=CodexSteer(self, response.turn.id),
        )

    async def handle_server_request(self, message: RpcMessage) -> JsonValue:
        if message.method in APPROVAL_METHODS:
            return await self.resolve_approval(message)
        if message.method == "mcpServer/elicitation/request":
            return self.resolve_mcp_elicitation(message)
        if message.method != "item/tool/call":
            raise RuntimeError(f"unsupported app-server request {message.method!r}")
        call = DynamicToolCall.model_validate(message.params)
        submission = self.submission
        if submission is None or call.tool != SUBMISSION_TOOL:
            return {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "No matching turn output binding is active.",
                    }
                ],
                "success": False,
            }
        if self.channel is None or self.channel.turn_id != call.turn_id:
            return {
                "contentItems": [
                    {"type": "inputText", "text": "Submission belongs to a stale turn."}
                ],
                "success": False,
            }
        response = await submission.submit(call.arguments)
        return {
            "contentItems": [{"type": "inputText", "text": response.message}],
            "success": response.accepted,
        }

    async def resolve_approval(self, message: RpcMessage) -> JsonValue:
        """Answer one approval request from this session's declared hooks.

        A session with no hooks declines rather than accepting. Reaching here
        at all means the thread was started under a policy that asks, and the
        constructor refuses that combination — so this is the belt to that
        validator's braces, and the safe answer to a question nobody can
        answer is no.
        """
        if self.config.hooks is None:
            return {"decision": "decline"}
        responder = CodexApprovalResponder(hooks=self.config.hooks)
        method = message.method or ""
        return {"decision": await responder.decide(method, message.params)}

    def resolve_mcp_elicitation(self, message: RpcMessage) -> JsonValue:
        """Accept tool-call elicitations for servers this session composed.

        The composition that opened this session declared its ``mcp_servers``,
        so calls to those servers are pre-authorized; an elicitation naming
        any other server declines. Without this, every project tool call is
        reported to the model as "user rejected MCP tool call".
        """
        request = McpElicitationRequest.model_validate(message.params)
        if request.server_name in self.config.mcp_servers:
            return {"action": "accept"}
        return {"action": "decline"}

    def handle_notification(self, notification: RpcNotification) -> None:
        if self.channel is not None:
            self.channel.feed(notification)

    def handle_disconnect(self, error: Exception) -> None:
        if self.channel is not None:
            self.channel.fail(error)


class CodexTurnToolBinder(TurnToolBinder):
    """Refresh A-to-A handler state and reject unsafe schema transitions."""

    def __init__(self, state: CodexConversationState) -> None:
        self.state = state

    async def bind[T: BaseModel](self, binding: TurnToolBinding[T] | None) -> None:
        submission = bound_submission(binding) if binding is not None else None
        digest = submission.digest if submission is not None else None
        if self.state.schema_digest is not None and digest != self.state.schema_digest:
            raise CodexSchemaRebindingError(
                "the installed Codex app-server exposes dynamicTools only on "
                "thread/start; changing or removing submit_output would lose "
                "conversation identity"
            )
        self.state.submission = submission
        self.state.schema_digest = digest


class CodexInterrupt(Interrupt):
    def __init__(self, state: CodexConversationState, turn_id: str) -> None:
        self.state = state
        self.turn_id = turn_id

    async def interrupt(self) -> None:
        thread_id = await self.state.ensure_thread()
        await self.state.server.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": self.turn_id}
        )


class CodexSteer(Steer):
    def __init__(self, state: CodexConversationState, turn_id: str) -> None:
        self.state = state
        self.turn_id = turn_id

    async def steer(self, input: TurnInput) -> None:
        thread_id = await self.state.ensure_thread()
        await self.state.server.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "turnId": self.turn_id,
                "input": [{"type": "text", "text": input.text}],
            },
        )


class CodexFork(ForkSession):
    def __init__(self, state: CodexConversationState) -> None:
        self.state = state

    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager[SessionHandle]:
        return self.open_fork(at)

    @asynccontextmanager
    async def open_fork(self, at: TurnId | None) -> AsyncGenerator[SessionHandle]:
        if at is not None:
            raise ValueError("Codex thread/fork can only fork the latest thread state")
        thread_id = await self.state.ensure_thread()
        result = await self.state.server.request("thread/fork", {"threadId": thread_id})
        response = CodexThreadResponse.model_validate(result)
        opener = CodexSessionOpener(self.state.config)
        async with opener.open_session(SessionId(value=response.thread.id)) as handle:
            yield handle


class CodexSessionOpener:
    """Open one initialized app-server process per Lup session."""

    def __init__(self, config: CodexSessionConfig) -> None:
        self.config = config

    @asynccontextmanager
    async def open_session(
        self, resume: SessionId | None = None
    ) -> AsyncGenerator[SessionHandle]:
        server = CodexAppServer(
            self.config.executable,
            arguments=(
                ["--profile", self.config.named_profile]
                if self.config.named_profile is not None
                else None
            ),
            environment=self.config.environment,
        )
        await server.start()
        state = CodexConversationState(self.config, server, resume)
        session = ComposedSession(
            state.start_turn,
            CodexTurnToolBinder(state),
            gate_resolver=self.config.submission_gate_resolver,
            submission_tool=SUBMISSION_TOOL,
        )
        try:
            yield SessionHandle(session=session, fork=CodexFork(state))
        finally:
            try:
                await session.abort_active()
            finally:
                await server.close()


# lup: ignore[model-free-function] — composition root over the session config
def create_codex_session_factory(config: CodexSessionConfig) -> SessionFactory:
    """Create the named Codex runtime composition root."""
    return SessionFactory(CodexSessionOpener(config).open_session)


def dynamic_tool(submission: TurnSubmission) -> JsonObject:
    """Render the exact Pydantic schema into the experimental native tool spec."""
    return {
        "name": SUBMISSION_TOOL,
        "description": "Submit the final validated result for this turn.",
        "inputSchema": submission.schema,
        "deferLoading": False,
    }


def decode_usage(payload: JsonObject) -> Usage:
    """Decode one app-server token-usage breakdown."""
    native = TokenUsageBreakdown.model_validate(payload)
    return Usage(
        input_tokens=native.input_tokens,
        output_tokens=native.output_tokens,
        cache_read_input_tokens=native.cached_input_tokens,
    )


def message_role(payload: JsonObject) -> Literal["user", "assistant", "tool", "system"]:
    """Which transcript role one completed item belongs to.

    A tool call and its result are the model's own act and the environment's
    reply, and collapsing both into one assistant message is what made a
    trace unable to show a call beside the result it produced.
    """
    match payload:
        case (
            {"type": "commandExecution"}
            | {"type": "fileChange"}
            | {"type": "mcpToolCall"}
        ):
            return "tool"
        case _:
            return "assistant"


def decode_completed_item(payload: JsonObject) -> list[AnyTurnBlock]:
    """Decode one typed completed app-server item into canonical blocks."""
    from lup.runtime.models import (
        TurnTextBlock,
        TurnThinkingBlock,
        TurnToolCallBlock,
        TurnToolResultBlock,
    )

    match payload:
        case {"type": "agentMessage", "text": str(text)}:
            return [TurnTextBlock(text=text)]
        case {"type": "reasoning", "content": list(content)}:
            return [
                TurnThinkingBlock(thinking="\n".join(str(item) for item in content))
            ]
        case {
            "type": "commandExecution",
            "id": str(identifier),
            "command": str(command),
            "aggregatedOutput": output,
            "status": status,
        }:
            blocks: list[AnyTurnBlock] = [
                TurnToolCallBlock(
                    id=identifier,
                    name="ShellCommand",
                    arguments={"command": command},
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=output if isinstance(output, str) else "",
                    is_error=status != "completed",
                ),
            ]
            return blocks
        case {
            "type": "fileChange",
            "id": str(identifier),
            "changes": list(changes),
            "status": status,
        }:
            blocks = [
                TurnToolCallBlock(
                    id=identifier,
                    name="EditBatch",
                    arguments={"changes": changes},
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=str(status),
                    is_error=status != "completed",
                ),
            ]
            return blocks
        case {
            "type": "mcpToolCall",
            "id": str(identifier),
            "server": str(server),
            "tool": str(tool),
            "arguments": arguments,
            "status": status,
        }:
            encoded_arguments: JsonObject = (
                {str(key): value for key, value in arguments.items()}
                if isinstance(arguments, dict)
                else {"value": arguments}
            )
            blocks = [
                TurnToolCallBlock(
                    id=identifier,
                    name=f"mcp__{server}__{tool}",
                    arguments=encoded_arguments,
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=json.dumps(payload, sort_keys=True),
                    is_error=status != "completed",
                ),
            ]
            return blocks
        case {
            "type": "dynamicToolCall",
            "id": str(identifier),
            "tool": str(tool),
            "arguments": arguments,
            "status": status,
        }:
            encoded_arguments: JsonObject = (
                {str(key): value for key, value in arguments.items()}
                if isinstance(arguments, dict)
                else {"value": arguments}
            )
            blocks = [
                TurnToolCallBlock(
                    id=identifier,
                    name=tool,
                    arguments=encoded_arguments,
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=json.dumps(payload, sort_keys=True),
                    is_error=status != "completed",
                ),
            ]
            return blocks
        case _:
            return []
