"""Bounding a Read that named no bound.

An unbounded Read is the one call that can spend a whole context window in
a single step, and the agent cannot avoid it by being careful: the size it
would have needed to know is what the call returns. These pin the hook that
corrects the arguments instead of refusing the call.
"""

from lup.hooks import LupHookInput, LupHooksConfig, create_large_read_hook
from lup.types import JsonObject


async def rewrite_for(
    config: LupHooksConfig,
    tool_name: str,
    tool_input: JsonObject,
) -> JsonObject | None:
    """The corrected arguments the hook produced, or None if it declined."""
    matcher = config.pre_tool_use[0]
    output = await matcher.hook(
        LupHookInput(
            event="PreToolUse",
            tool_name=tool_name,
            tool_input=tool_input,
        )
    )
    return output.updated_input


async def test_a_read_without_a_limit_is_given_the_default() -> None:
    config = create_large_read_hook()

    rewritten = await rewrite_for(config, "Read", {"file_path": "/a/b.txt"})

    assert rewritten == {"file_path": "/a/b.txt", "limit": 2000}


async def test_the_limit_is_the_caller_s_judgement_not_the_library_s() -> None:
    config = create_large_read_hook(default_limit=50)

    rewritten = await rewrite_for(config, "Read", {"file_path": "/a/b.txt"})

    assert rewritten == {"file_path": "/a/b.txt", "limit": 50}


async def test_a_read_that_chose_its_own_limit_keeps_it() -> None:
    config = create_large_read_hook()

    rewritten = await rewrite_for(
        config, "Read", {"file_path": "/a/b.txt", "limit": 40_000}
    )

    assert rewritten is None


async def test_the_rest_of_the_call_survives_the_rewrite() -> None:
    config = create_large_read_hook()

    rewritten = await rewrite_for(
        config, "Read", {"file_path": "/a/b.txt", "offset": 900}
    )

    assert rewritten == {"file_path": "/a/b.txt", "offset": 900, "limit": 2000}


async def test_no_other_tool_is_touched() -> None:
    config = create_large_read_hook()

    assert await rewrite_for(config, "Grep", {"path": "/src"}) is None
    assert await rewrite_for(config, "Write", {"file_path": "/a/b.txt"}) is None


async def test_the_hook_decides_nothing_it_was_not_asked_to_decide() -> None:
    config = create_large_read_hook()
    matcher = config.pre_tool_use[0]

    output = await matcher.hook(
        LupHookInput(
            event="PreToolUse", tool_name="Read", tool_input={"file_path": "/a/b.txt"}
        )
    )

    # Correcting a call is not permitting it; the permission flow still runs.
    assert output.decision is None
    assert output.reason == ""


async def test_only_the_event_that_still_has_an_input_to_rewrite() -> None:
    config = create_large_read_hook()
    matcher = config.pre_tool_use[0]

    output = await matcher.hook(
        LupHookInput(
            event="PostToolUse", tool_name="Read", tool_input={"file_path": "/a/b.txt"}
        )
    )

    assert output.updated_input is None
