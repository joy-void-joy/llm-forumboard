# lup: ignore[constant-declaration]
# Every constant here is a Codex app-server method or reply word, so the wire
# decides the value and a caller passing another would answer a request the
# server never sends.
"""Translate backend-neutral Lup hooks to Codex app-server approval replies.

The Claude twin of this module wraps SDK hook handlers. Codex has no such
registry: the seam it offers is the approval request the app-server sends
*back* to whoever opened the thread, once that thread was started under a
policy that asks. So a portable hook becomes a reply to a server-initiated
request, and the transport in :mod:`lup.adapters.codex.app_server` already
routes every such request to one installed handler.

What each boundary carries decides what can be judged there, and the two
approval requests are not alike:

``item/commandExecution/requestApproval``
    carries the command and its working directory, so a shell rule is judged
    on exactly the text about to run. This is parity with the Claude path.

``item/fileChange/requestApproval``
    carries an item id, a reason, and a root — and no file content at all. An
    edit rule reads before-and-after text, so there is nothing here for one to
    read. The request is therefore decoded as the opaque operation it is and
    answered by whatever the policy does with an unknown tool, which for every
    policy this library ships is a refusal. Approving it because the content
    could not be inspected would be the one reading that turns a missing
    capability into a silent grant.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field

from lup.adapters.codex.native import (
    CodexBeforeToolEvent,
    CodexEventDecoder,
    CodexShellOperation,
    CodexUnknownOperation,
)
from lup.hooks import LupHookInput, LupHooksConfig
from lup.policy.enforcement import NativeSemantics
from lup.policy.models import SemanticTool
from lup.types import JsonObject

COMMAND_APPROVAL = "item/commandExecution/requestApproval"
"""The app-server request carrying a command about to run."""

FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
"""The app-server request carrying a patch about to apply, minus its content."""

APPROVAL_METHODS = (COMMAND_APPROVAL, FILE_CHANGE_APPROVAL)
"""Every server request this seam answers."""

ACCEPT = "accept"
DECLINE = "decline"
"""The two decisions this seam ever returns.

``acceptForSession`` is deliberately never returned: it would grant every
later request of the same shape without judging it, which is the opposite of
what a per-call policy is for. ``cancel`` belongs to a human abandoning a
turn, and nothing here is a human.
"""


class CodexCommandApproval(BaseModel, frozen=True, extra="ignore"):
    """The fields of a command-execution approval this seam reads.

    Codex sends more than these — item and turn identifiers, the decisions it
    would accept, execpolicy amendment offers. They are ignored rather than
    modelled, because a policy judges the act and not the bookkeeping around
    it, and an unmodelled field cannot break validation when Codex adds one.
    """

    command: str = ""
    cwd: Path | None = None


def codex_approval_event(method: str, params: JsonObject) -> CodexBeforeToolEvent:
    """Decode one approval request into the operation it asks about.

    A command approval becomes the shell operation it names. Anything else — a
    file change, whose content this boundary does not carry, or a method this
    library has never seen — becomes the opaque operation it is, so the
    policy's unknown-tool arm answers for it rather than this function
    inventing a shape nobody sent.
    """
    if method == COMMAND_APPROVAL:
        approval = CodexCommandApproval.model_validate(params)
        return CodexBeforeToolEvent(
            operation=CodexShellOperation(command=approval.command, cwd=approval.cwd)
        )
    return CodexBeforeToolEvent(
        operation=CodexUnknownOperation(name=method, input=params)
    )


def codex_approval_semantic_tool(event: LupHookInput) -> SemanticTool:
    """Decode one approval request into the tool a semantic policy judges."""
    return (
        CodexEventDecoder()
        .decode(codex_approval_event(event.tool_name, event.tool_input))
        .tool
    )


CODEX_SEMANTICS = NativeSemantics(
    decode=codex_approval_semantic_tool,
    routed_tools=list(APPROVAL_METHODS),
    agent_escalates=True,
)
"""What an in-process Codex session hands a semantic policy.

The routed set is the approval methods themselves, because that is the whole
vocabulary this boundary speaks: unlike the Claude hook, which sees a tool
roster, the app-server asks about acts.

That is also why the two sandbox facts split here. An approval reply accepts
or declines and rewrites nothing, so no verdict of this seam's places a call
and ``escapable`` stays false. The agent's own escape is a different matter
and it has one: Codex puts ``sandbox_permissions`` on the shell tool the
model calls — see :meth:`~lup.adapters.codex.harness.CodexSpellings.escape_sandbox`
for the source it was read from.
"""


class CodexApprovalResponder(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Answer app-server approval requests from portable hook registrations.

    One of these exists per conversation, because the transport installs one
    handler for the whole session and a session's hooks are fixed when it
    opens.
    """

    hooks: LupHooksConfig = Field(description="What this session declared")

    def handles(self, method: str) -> bool:
        """Whether an approval request is one this responder answers."""
        return method in APPROVAL_METHODS

    def hook_input(self, method: str, params: JsonObject) -> LupHookInput:
        """Present one approval request in the portable hook vocabulary."""
        approval = (
            CodexCommandApproval.model_validate(params)
            if method == COMMAND_APPROVAL
            else None
        )
        return LupHookInput(
            event="PreToolUse",
            tool_name=method,
            tool_input=params,
            tool_path=str(approval.cwd) if approval and approval.cwd else "",
        )

    async def decide(self, method: str, params: JsonObject) -> str:
        """Run every registered PreToolUse hook and reply with one decision.

        An ``ask`` reaching here has nobody to ask: the app-server is a
        program and this session was opened without a human attached, so it
        declines carrying the reason rather than approving something whose
        approval was never given. That is the same fail-closed reading the
        generated Codex dispatcher takes, reached the same way.
        """
        outputs = [
            await matcher.hook(self.hook_input(method, params))
            for matcher in self.hooks.pre_tool_use
        ]
        refused = [
            item for item in outputs if item.decision in ("deny", "block", "ask")
        ]
        return DECLINE if refused else ACCEPT


type ApprovalHandler = Callable[[str, JsonObject], Awaitable[str]]


def build_codex_approval_handler(hooks: LupHooksConfig) -> ApprovalHandler:
    """Close one approval responder over a session's portable hooks."""
    responder = CodexApprovalResponder(hooks=hooks)

    async def respond(method: str, params: JsonObject) -> str:
        return await responder.decide(method, params)

    return respond
