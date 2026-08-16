# lup: ignore[own-model-dispatch]
# The Codex*Operation models mirror Codex app-server and hook payloads — the
# apply_patch file-change list, Bash, web_fetch, web_search — so the arms of
# CodexEventDecoder.decode narrow a vendor payload rather than dispatch on a
# union of ours. Answering `decode` from each mirror would pull the neutral
# lup.policy vocabulary back across the boundary this adapter exists to hold,
# and would make the vendor's tool roster, not ours, decide when a variant is
# added.
"""Codex-private native event parsing and capability-aware decisions."""

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, ValidationError

from lup.policy.models import (
    BeforeTool,
    Decision,
    EditBatch,
    EditChange,
    FetchUrl,
    SearchWeb,
    ShellCommand,
    ToolIdentity,
    UnknownTool,
)
from lup.policy.native import NativeDecisionRenderer, NativeEventDecoder
from lup.types import JsonObject


class CodexFileChange(BaseModel, frozen=True):
    path: Path
    before: str | None = None
    after: str | None = None


class CodexFileChangeOperation(BaseModel, frozen=True):
    type: Literal["file_change"] = "file_change"
    changes: list[CodexFileChange] = Field(min_length=1)


class CodexShellOperation(BaseModel, frozen=True):
    type: Literal["shell"] = "shell"
    command: str
    cwd: Path | None = None


class CodexFetchOperation(BaseModel, frozen=True):
    type: Literal["fetch"] = "fetch"
    url: str


class CodexSearchOperation(BaseModel, frozen=True):
    type: Literal["search"] = "search"
    query: str


class CodexUnknownOperation(BaseModel, frozen=True):
    type: Literal["unknown"] = "unknown"
    name: str
    input: JsonObject = {}


type CodexOperation = (
    CodexFileChangeOperation
    | CodexShellOperation
    | CodexFetchOperation
    | CodexSearchOperation
    | CodexUnknownOperation
)


class CodexBeforeToolEvent(BaseModel, frozen=True):
    operation: CodexOperation


class CodexHookPayload(BaseModel, frozen=True):
    """Validated external hook input before operation-specific parsing."""

    tool_name: str
    tool_input: JsonObject = {}


# lup: ignore[model-free-function] — boundary decoder off Codex's wire payload
def parse_codex_before_tool(payload: CodexHookPayload) -> CodexBeforeToolEvent:
    """Decode stable Codex hook fields; opaque patches remain conservative."""
    match payload.tool_name, payload.tool_input:
        case "Bash", {"command": str(command)}:
            operation: CodexOperation = CodexShellOperation(command=command)
        case "web_fetch", {"url": str(url)}:
            operation = CodexFetchOperation(url=url)
        case "web_search", {"query": str(query)}:
            operation = CodexSearchOperation(query=query)
        case _:
            operation = CodexUnknownOperation(
                name=payload.tool_name, input=payload.tool_input
            )
    return CodexBeforeToolEvent(operation=operation)


class CodexEventDecoder(NativeEventDecoder[CodexBeforeToolEvent]):
    """Decode Codex app-server/hook operations into shared semantic events."""

    def decode(self, event: CodexBeforeToolEvent) -> BeforeTool:
        operation = event.operation
        match operation:
            case CodexFileChangeOperation(changes=changes):
                tool = EditBatch(
                    changes=[
                        EditChange(
                            path=change.path,
                            before=change.before,
                            after=change.after,
                        )
                        for change in changes
                    ]
                )
                name = "apply_patch"
            case CodexShellOperation(command=command, cwd=cwd):
                tool = ShellCommand(command=command, cwd=cwd)
                name = "Bash"
            case CodexFetchOperation(url=url):
                name = "web_fetch"
                try:
                    tool = FetchUrl(url=AnyHttpUrl(url))
                except ValidationError:
                    identity = ToolIdentity(original_name=name)
                    return BeforeTool(
                        tool=UnknownTool(identity=identity, input={"url": url}),
                        identity=identity,
                    )
            case CodexSearchOperation(query=query):
                tool = SearchWeb(query=query)
                name = "web_search"
            case CodexUnknownOperation(name=name, input=input_data):
                identity = ToolIdentity(original_name=name)
                return BeforeTool(
                    tool=UnknownTool(identity=identity, input=input_data),
                    identity=identity,
                )
        identity = ToolIdentity(original_name=name)
        return BeforeTool(tool=tool, identity=identity)


class CodexDecisionOutput(BaseModel, frozen=True):
    """Exit behavior for one hermetic Codex command hook."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    approximation: str | None = None


class CodexDecisionRenderer(NativeDecisionRenderer[CodexDecisionOutput]):
    """Render approval when supported and otherwise fail closed.

    A placement is degraded away here, because what this boundary returns is
    an accept or a decline and nothing else: the call it judges runs with the
    arguments the model wrote, so a verdict of its own has no way to move one
    outside the sandbox. Saying so is what keeps a placement from reading as
    honoured — dropped in silence, an escape this boundary cannot perform
    would look performed to everything upstream of it. Codex's agent requests
    ``outside`` on its call; :func:`~lup.adapters.codex.harness.codex_allow_prefixes`
    compiles the approval for safe outside commands, not the placement itself.

    A permission to escalate survives the degrading, because it was never
    addressed to this boundary: the agent spends it on its own next call, with
    the words :meth:`~lup.adapters.codex.harness.CodexSpellings.escape_sandbox`
    carries.
    """

    def __init__(self, supports_ask: bool) -> None:
        if supports_ask:
            raise ValueError(
                "native Codex approval rendering has not been evidenced; "
                "ask must remain fail-closed"
            )
        self.supports_ask = supports_ask

    def render(
        self, decision: Decision, tool_input: JsonObject | None = None
    ) -> CodexDecisionOutput:
        settled = decision.placed(escapable=False, agent_escalates=True)
        match settled.effect:
            case "allow" | "defer":
                return CodexDecisionOutput(exit_code=0)
            case "deny":
                return CodexDecisionOutput(exit_code=2, stderr=settled.reason)
            case "ask":
                return CodexDecisionOutput(
                    exit_code=2,
                    stderr=(
                        f"Approval is required but unavailable at this boundary: "
                        f"{settled.reason}"
                    ),
                    approximation="ask rendered as fail-closed denial",
                )
