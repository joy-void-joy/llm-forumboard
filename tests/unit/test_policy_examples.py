"""The shipped policy examples refuse, driven as a session would drive them.

`examples/semantic_policy` and `examples/semantic_policy_shell` claim that a
denied call never reaches the provider. Each example's own session
configuration is built here and its registered hooks are invoked with the
payloads the SDK sends — every step of a live session except the model call.
An example nothing executes decays into a wrong demonstration, so the claim
is checked on each run instead of the day a reader tries it.
"""

from typing import Literal

from claude_agent_sdk import types as claude_types

from examples import semantic_policy, semantic_policy_shell
from lup.adapters.claude.runtime import ClaudeSessionConfig, build_claude_options
from lup.policy.kernel.decision import ESCALATE_HINT
from lup.types import JsonObject

ALLOWED_URL = "https://docs.example.com/api/runtime"


async def attempted_call(
    config: ClaudeSessionConfig, tool_name: str, tool_input: JsonObject
) -> claude_types.HookJSONOutput:
    """Answer one attempted tool call through the session's own hooks."""
    options = build_claude_options(
        config, binding=lambda: None, resume=None, session_id=None
    )
    assert options.hooks is not None, "the example session registers no hooks"
    payload = claude_types.PreToolUseHookInput(
        hook_event_name="PreToolUse",
        session_id="session",
        transcript_path="/transcript",
        cwd="/cwd",
        tool_name=tool_name,
        tool_input=dict(tool_input),
        tool_use_id="use-1",
    )
    decisions = [
        await hook(payload, "use-1", claude_types.HookContext(signal=None))
        for matcher in options.hooks["PreToolUse"]
        for hook in matcher.hooks
    ]
    assert len(decisions) == 1
    return decisions[0]


def permission(
    decision: Literal["allow", "ask", "deny"], reason: str
) -> claude_types.SyncHookJSONOutput:
    """The whole native answer, so a verdict on another channel fails here."""
    return claude_types.SyncHookJSONOutput(
        hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
            hookEventName="PreToolUse",
            permissionDecision=decision,
            permissionDecisionReason=reason,
        )
    )


async def test_fetch_example_refuses_the_url_its_policy_denies() -> None:
    decision = await attempted_call(
        semantic_policy.session_config(),
        "WebFetch",
        {"url": semantic_policy.DENIED_URL},
    )

    assert decision == permission("deny", "URL is denied")


async def test_fetch_example_allows_the_scope_it_declares() -> None:
    decision = await attempted_call(
        semantic_policy.session_config(), "WebFetch", {"url": ALLOWED_URL}
    )

    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


async def test_fetch_example_asks_before_a_family_it_never_declared() -> None:
    decision = await attempted_call(
        semantic_policy.session_config(), "Bash", {"command": "git status"}
    )

    assert decision == permission(
        "ask", "no shell policy is declared, so this call needs approval"
    )


async def test_shell_example_refuses_the_command_its_policy_denies() -> None:
    decision = await attempted_call(
        semantic_policy_shell.session_config(),
        "Bash",
        {"command": semantic_policy_shell.DENIED_COMMAND},
    )

    assert decision == permission("deny", f"URL is denied{ESCALATE_HINT}")


async def test_shell_example_allows_a_read_only_command() -> None:
    decision = await attempted_call(
        semantic_policy_shell.session_config(), "Bash", {"command": "ls -la"}
    )

    # An allow grants and says nothing further: the portable vocabulary
    # carries a reason only where one changes what the agent does next, and a
    # command whose rule states no placement is not rewritten either.
    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


async def test_shell_example_runs_a_git_read_outside_the_sandbox() -> None:
    """`git` states its placement once, so every verb beneath it escapes.

    The declaration sits on the command and this is a subcommand under it,
    which is what makes the round trip worth driving: the erased row already
    carries the placement, so the renderer reads one field rather than walking
    a hierarchy it cannot see.
    """
    command = "git status"

    decision = await attempted_call(
        semantic_policy_shell.session_config(), "Bash", {"command": command}
    )

    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": command, "dangerouslyDisableSandbox": True},
        }
    }


async def test_shell_example_asks_before_a_destructive_command() -> None:
    decision = await attempted_call(
        semantic_policy_shell.session_config(), "Bash", {"command": "rm -rf build"}
    )

    assert decision == permission("ask", "deleting files requires approval")
