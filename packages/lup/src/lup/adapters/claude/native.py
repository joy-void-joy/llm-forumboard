# lup: ignore[own-model-dispatch]
# The Claude*Operation models mirror Claude Code's PreToolUse tool_input shapes
# — Edit, Write, Bash, WebFetch, WebSearch — so the arms of
# ClaudeEventDecoder.decode narrow a vendor payload rather than dispatch on a
# union of ours. Answering `decode` from each mirror would pull the neutral
# lup.policy vocabulary back across the boundary this adapter exists to hold,
# and would make the vendor's tool roster, not ours, decide when a variant is
# added.
"""Claude-private native event parsing and decision rendering."""

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
from lup.policy.kernel.decision import (
    SandboxPlacement,
    escalation_offer,
    sandbox_escaped,
)
from lup.policy.native import NativeDecisionRenderer, NativeEventDecoder
from lup.types import JsonObject


class ClaudeEditOperation(BaseModel, frozen=True):
    type: Literal["edit"] = "edit"
    path: Path
    before: str
    after: str


class ClaudeWriteOperation(BaseModel, frozen=True):
    type: Literal["write"] = "write"
    path: Path
    content: str


class ClaudeEditBatchOperation(BaseModel, frozen=True):
    type: Literal["edit_batch"] = "edit_batch"
    changes: list[EditChange] = Field(min_length=1)


class ClaudeShellOperation(BaseModel, frozen=True):
    type: Literal["shell"] = "shell"
    command: str
    cwd: Path | None = None
    unsandboxed: bool = False


class ClaudeFetchOperation(BaseModel, frozen=True):
    type: Literal["fetch"] = "fetch"
    url: str


class ClaudeSearchOperation(BaseModel, frozen=True):
    type: Literal["search"] = "search"
    query: str


class ClaudeUnknownOperation(BaseModel, frozen=True):
    type: Literal["unknown"] = "unknown"
    name: str
    input: JsonObject = {}


type ClaudeOperation = (
    ClaudeEditOperation
    | ClaudeWriteOperation
    | ClaudeEditBatchOperation
    | ClaudeShellOperation
    | ClaudeFetchOperation
    | ClaudeSearchOperation
    | ClaudeUnknownOperation
)


class ClaudeBeforeToolEvent(BaseModel, frozen=True):
    operation: ClaudeOperation


class ClaudeHookPayload(BaseModel, frozen=True):
    """Validated external hook input before operation-specific parsing."""

    tool_name: str
    tool_input: JsonObject = {}


# lup: ignore[model-free-function] — boundary decoder off Claude's wire payload
def parse_claude_before_tool(payload: ClaudeHookPayload) -> ClaudeBeforeToolEvent:
    """Decode Claude names and payload fields at the adapter boundary."""
    match payload.tool_name, payload.tool_input:
        case "Edit", {
            "file_path": str(path),
            "old_string": str(before),
            "new_string": str(after),
        }:
            operation: ClaudeOperation = ClaudeEditOperation(
                path=Path(path), before=before, after=after
            )
        case "Write", {"file_path": str(path), "content": str(content)}:
            operation = ClaudeWriteOperation(path=Path(path), content=content)
        case "Bash", {"command": str(command), "dangerouslyDisableSandbox": True}:
            operation = ClaudeShellOperation(command=command, unsandboxed=True)
        case "Bash", {"command": str(command)}:
            operation = ClaudeShellOperation(command=command)
        case "WebFetch", {"url": str(url)}:
            operation = ClaudeFetchOperation(url=url)
        case "WebSearch", {"query": str(query)}:
            operation = ClaudeSearchOperation(query=query)
        case _:
            operation = ClaudeUnknownOperation(
                name=payload.tool_name, input=payload.tool_input
            )
    return ClaudeBeforeToolEvent(operation=operation)


class ClaudeEventDecoder(NativeEventDecoder[ClaudeBeforeToolEvent]):
    """Decode validated Claude operations into the shared vocabulary."""

    def decode(self, event: ClaudeBeforeToolEvent) -> BeforeTool:
        operation = event.operation
        match operation:
            case ClaudeEditOperation(path=path, before=before, after=after):
                tool = EditBatch(
                    changes=[EditChange(path=path, before=before, after=after)]
                )
                name = "Edit"
            case ClaudeWriteOperation(path=path, content=content):
                tool = EditBatch(changes=[EditChange(path=path, after=content)])
                name = "Write"
            case ClaudeEditBatchOperation(changes=changes):
                tool = EditBatch(changes=changes)
                name = "Edit"
            case ClaudeShellOperation(
                command=command, cwd=cwd, unsandboxed=unsandboxed
            ):
                tool = ShellCommand(command=command, cwd=cwd, unsandboxed=unsandboxed)
                name = "Bash"
            case ClaudeFetchOperation(url=url):
                name = "WebFetch"
                try:
                    tool = FetchUrl(url=AnyHttpUrl(url))
                except ValidationError:
                    identity = ToolIdentity(original_name=name)
                    return BeforeTool(
                        tool=UnknownTool(identity=identity, input={"url": url}),
                        identity=identity,
                    )
            case ClaudeSearchOperation(query=query):
                tool = SearchWeb(query=query)
                name = "WebSearch"
            case ClaudeUnknownOperation(name=name, input=input_data):
                identity = ToolIdentity(original_name=name)
                return BeforeTool(
                    tool=UnknownTool(identity=identity, input=input_data),
                    identity=identity,
                )
        identity = ToolIdentity(original_name=name)
        return BeforeTool(tool=tool, identity=identity)


def claude_sandbox_input(
    tool_input: JsonObject | None, sandbox: SandboxPlacement
) -> JsonObject | None:
    """The call's own arguments, rewritten to run where the verdict placed it.

    Claude Code's one spelling of the sandbox axis, and the only place it is
    written. The rewrite replaces the arguments outright rather than merging
    into them, which is why the whole input is carried through; an unplaced
    verdict rewrites nothing at all.

    Which placements leave is :func:`~lup.policy.kernel.decision.sandbox_escaped`
    and not this function, because the compiled dispatcher renders the same
    rewrite and cannot import this one. What stays here is the field name,
    which is Claude Code's own and reaches no other runtime.
    """
    if tool_input is None or sandbox == "ambient":
        return None
    match tool_input:
        case {"dangerouslyDisableSandbox": True}:
            spent = True
        case _:
            spent = False
    escaped = sandbox_escaped(sandbox, spent)
    return {**tool_input, "dangerouslyDisableSandbox": escaped}


class ClaudeDecisionOutput(BaseModel, frozen=True, populate_by_name=True):
    """Claude PreToolUse hook-specific decision payload."""

    hook_event_name: Literal["PreToolUse"] = Field(
        default="PreToolUse", alias="hookEventName"
    )
    permission_decision: Literal["allow", "ask", "deny"] | None = Field(
        default=None, alias="permissionDecision"
    )
    reason: str = Field(default="", alias="permissionDecisionReason")
    updated_input: JsonObject | None = Field(default=None, alias="updatedInput")
    additional_context: str = Field(default="", alias="additionalContext")
    """What the agent reads, as against what the human asked is shown.

    The two are separate channels and a grant only travels on this one: a
    permission reason on an allow reaches the user, so a verdict with
    something for the agent to act on has to say it here as well."""


class ClaudeDecisionRenderer(NativeDecisionRenderer[ClaudeDecisionOutput]):
    """Render semantic effects; defer omits the decision so the client mode applies.

    Claude Code takes a call's sandbox as an argument of the call, so a placed
    verdict goes out as the permission decision plus a rewrite of the
    arguments. That rewrite is what makes an unprompted placement reachable at
    all, and the rewrite channel carries it: the hook schema types it as an
    open record, the flag is a declared field of the shell tool's own input
    schema rather than an unknown key the validation would reject, and the one
    per-tool key filter applied before execution names a different tool
    entirely — so the object arrives whole and the sandbox is chosen from it.
    Read out of the shipped binary at version 2.1.228; the compiled dispatcher
    in ``assets/policy_dispatcher.py`` carries the finding in full.
    """

    def render(
        self, decision: Decision, tool_input: JsonObject | None = None
    ) -> ClaudeDecisionOutput:
        settled = decision.placed(escapable=True, agent_escalates=True)
        if settled.effect == "defer":
            return ClaudeDecisionOutput(permissionDecisionReason=settled.reason)
        return ClaudeDecisionOutput(
            permissionDecision=settled.effect,
            permissionDecisionReason=settled.reason,
            updatedInput=claude_sandbox_input(tool_input, settled.sandbox),
            additionalContext=escalation_offer(settled.sandbox, settled.reason),
        )
