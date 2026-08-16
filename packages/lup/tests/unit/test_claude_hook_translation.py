"""The Claude adapter's hook seam: portable hooks rendered as native SDK hooks.

`lup.adapters.claude.hooks` is the boundary every permission and gate hook
crosses on the Claude backend. These pin the translation contract: each
path-bearing tool normalizes to one ``tool_path``, a call decodes to the
semantic tool a policy judges, structured tool responses arrive as JSON
text, every PreToolUse verdict answers on the permission channel while
other events answer on the decision channel, and per-event matcher
registration survives the mapping.
"""

from claude_agent_sdk import types as claude_types
from pydantic import AnyHttpUrl

from lup.adapters.claude.hooks import (
    build_claude_hook_handler,
    claude_hook_semantic_tool,
    claude_hook_tool_path,
    lup_hook_output_to_claude,
    lup_hooks_to_claude,
)
from lup.hooks import LupHookInput, LupHookMatcher, LupHookOutput, LupHooksConfig
from lup.policy.models import FetchUrl, ShellCommand, ToolIdentity, UnknownTool
from lup.types import JsonObject


def test_tool_path_normalizes_each_path_bearing_tool() -> None:
    assert claude_hook_tool_path("Write", {"file_path": "/a/b.txt"}) == "/a/b.txt"
    assert claude_hook_tool_path("Edit", {"file_path": "/a/b.txt"}) == "/a/b.txt"
    assert claude_hook_tool_path("Read", {"file_path": "/a/b.txt"}) == "/a/b.txt"
    assert claude_hook_tool_path("Grep", {"path": "/src"}) == "/src"
    assert claude_hook_tool_path("WebSearch", {"query": "x"}) == ""


def test_glob_prefers_explicit_path_and_falls_back_to_pattern_dir() -> None:
    both: JsonObject = {"path": "/root", "pattern": "**/*.py"}
    assert claude_hook_tool_path("Glob", both) == "/root"
    # An empty path grants nothing; the pattern's static prefix is the target.
    empty_path: JsonObject = {"path": "", "pattern": "/docs/**/*.md"}
    assert claude_hook_tool_path("Glob", empty_path) == "/docs"
    assert claude_hook_tool_path("Glob", {"pattern": "/docs/**/*.md"}) == "/docs"


def test_semantic_decode_reads_the_same_names_the_dispatcher_reads() -> None:
    fetch = claude_hook_semantic_tool(
        LupHookInput(
            event="PreToolUse",
            tool_name="WebFetch",
            tool_input={"url": "https://docs.example.com/api"},
        )
    )
    assert fetch == FetchUrl(url=AnyHttpUrl("https://docs.example.com/api"))

    shell = claude_hook_semantic_tool(
        LupHookInput(
            event="PreToolUse", tool_name="Bash", tool_input={"command": "git status"}
        )
    )
    assert shell == ShellCommand(command="git status")

    unclassified = claude_hook_semantic_tool(
        LupHookInput(event="PreToolUse", tool_name="mcp__notes__write", tool_input={})
    )
    assert unclassified == UnknownTool(
        identity=ToolIdentity(original_name="mcp__notes__write")
    )


def pre_tool_use_input(
    tool_name: str, tool_input: JsonObject
) -> claude_types.PreToolUseHookInput:
    return claude_types.PreToolUseHookInput(
        hook_event_name="PreToolUse",
        session_id="session",
        transcript_path="/transcript",
        cwd="/cwd",
        tool_name=tool_name,
        tool_input=dict(tool_input),
        tool_use_id="use-1",
    )


async def test_handler_delivers_normalized_input_and_native_denial() -> None:
    received: list[LupHookInput] = []

    async def deny_hook(data: LupHookInput) -> LupHookOutput:
        received.append(data)
        return LupHookOutput(decision="deny", reason="outside the write grant")

    handler = build_claude_hook_handler(
        LupHookMatcher(hook=deny_hook), event="PreToolUse"
    )
    output = await handler(
        pre_tool_use_input("Write", {"file_path": "/ro/file.txt"}),
        "use-1",
        claude_types.HookContext(signal=None),
    )

    assert received == [
        LupHookInput(
            event="PreToolUse",
            tool_name="Write",
            tool_input={"file_path": "/ro/file.txt"},
            tool_path="/ro/file.txt",
        )
    ]
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "outside the write grant",
        }
    }


async def test_handler_serializes_structured_tool_responses_to_json_text() -> None:
    received: list[LupHookInput] = []

    async def record_hook(data: LupHookInput) -> LupHookOutput:
        received.append(data)
        return LupHookOutput()

    handler = build_claude_hook_handler(
        LupHookMatcher(hook=record_hook), event="PostToolUse"
    )
    native_input = claude_types.PostToolUseHookInput(
        hook_event_name="PostToolUse",
        session_id="session",
        transcript_path="/transcript",
        cwd="/cwd",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_response={"is_error": True},
        tool_use_id="use-2",
    )

    output = await handler(native_input, "use-2", claude_types.HookContext(signal=None))

    assert received[0].tool_result == '{"is_error": true}'
    assert output == {}


async def test_handler_forwards_stop_hook_active_to_the_portable_hook() -> None:
    received: list[LupHookInput] = []

    async def stop_guard(data: LupHookInput) -> LupHookOutput:
        received.append(data)
        return LupHookOutput(decision="block", reason="sleep before stopping")

    handler = build_claude_hook_handler(LupHookMatcher(hook=stop_guard), event="Stop")
    native_input = claude_types.StopHookInput(
        hook_event_name="Stop",
        session_id="session",
        transcript_path="/transcript",
        cwd="/cwd",
        stop_hook_active=True,
    )

    output = await handler(native_input, None, claude_types.HookContext(signal=None))

    assert received[0].stop_hook_active is True
    assert output == {"decision": "block", "reason": "sleep before stopping"}


def test_output_rendering_covers_every_decision_shape() -> None:
    allow = lup_hook_output_to_claude(
        LupHookOutput(decision="allow"), event="PreToolUse"
    )
    assert allow == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }

    ask = lup_hook_output_to_claude(
        LupHookOutput(decision="ask", reason="approval required"), event="PreToolUse"
    )
    assert ask == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "approval required",
        }
    }

    # PreToolUse carries every verdict on the permission channel:
    # PreToolUseHookSpecificOutput.permissionDecision is
    # Literal["allow", "deny", "ask", "defer"], and the hook documentation's
    # decision-control table assigns the top-level `decision` field to
    # UserPromptSubmit, PostToolUse, Stop, SubagentStop and PreCompact —
    # PreToolUse is absent from it. So a block-styled gate refuses here as a
    # denial carrying its corrective message, rather than answering on a
    # channel this event does not read.
    blocked = lup_hook_output_to_claude(
        LupHookOutput(decision="block", reason="halt"), event="PreToolUse"
    )
    assert blocked == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "halt",
        }
    }

    # Stop has no permission channel and no approval flow, so an ask there
    # fails closed onto the decision channel rather than passing through.
    stop_ask = lup_hook_output_to_claude(
        LupHookOutput(decision="ask", reason="approval required"), event="Stop"
    )
    assert stop_ask == {"decision": "block", "reason": "approval required"}

    nudge = lup_hook_output_to_claude(
        LupHookOutput(system_message="prefer the structured API"), event="PostToolUse"
    )
    assert nudge == {"systemMessage": "prefer the structured API"}

    passthrough = lup_hook_output_to_claude(LupHookOutput(), event="PostToolUse")
    assert passthrough == {}


def test_rewrite_carries_corrected_arguments_without_granting_the_call() -> None:
    corrected: JsonObject = {"file_path": "/a/b.txt", "limit": 2000}

    rewrite = lup_hook_output_to_claude(
        LupHookOutput(updated_input=corrected), event="PreToolUse"
    )

    # No permissionDecision in the rendered output: the arguments are fixed,
    # and the verdict is still the ambient flow's to make.
    assert rewrite == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": corrected,
        }
    }


def test_an_offered_escalation_keeps_both_the_placement_and_the_offer() -> None:
    """The one verdict that fills both channels must not lose either.

    An escalable grant is placed — so it carries a rewrite — and speaks to
    the agent — so it carries context. Those are separate arms, and an arm
    ordered on the context alone answers first for a verdict that has both,
    dropping the rewrite that is the placement's only channel. The offer
    would then be granted in words and unspendable in arguments.
    """
    placed: JsonObject = {
        "command": "toolchain --run",
        "dangerouslyDisableSandbox": True,
    }

    offered = lup_hook_output_to_claude(
        LupHookOutput(
            decision="allow", sandbox="escalable", additional_context="may leave"
        ),
        event="PreToolUse",
        placed_input=placed,
    )

    assert offered == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": placed,
            "additionalContext": "may leave",
        }
    }


def test_refusal_outranks_a_rewrite_riding_along_with_it() -> None:
    refused = lup_hook_output_to_claude(
        LupHookOutput(
            decision="deny",
            reason="outside the workspace",
            updated_input={"file_path": "/etc/passwd", "limit": 10},
        ),
        event="PreToolUse",
    )

    assert refused == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "outside the workspace",
        }
    }


def test_only_pre_tool_use_can_rewrite_an_input_that_has_not_run_yet() -> None:
    for event in ("PostToolUse", "Stop"):
        after = lup_hook_output_to_claude(
            LupHookOutput(updated_input={"limit": 2000}), event=event
        )
        assert after == {}


def test_registration_maps_events_and_matchers_without_inventing_entries() -> None:
    async def hook(data: LupHookInput) -> LupHookOutput:
        del data
        return LupHookOutput()

    config = LupHooksConfig(
        pre_tool_use=[LupHookMatcher(matcher="Bash", hook=hook)],
        stop=[LupHookMatcher(hook=hook)],
    )

    native = lup_hooks_to_claude(config)

    assert sorted(native) == ["PreToolUse", "Stop"]  # empty PostToolUse dropped
    assert native["PreToolUse"][0].matcher == "Bash"
    assert native["Stop"][0].matcher is None
    assert len(native["PreToolUse"][0].hooks) == 1
