"""Route a model name to one explicit, fully configured factory recipe."""

import asyncio
from pathlib import Path

from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.adapters.codex.runtime import CodexSessionConfig, create_codex_session_factory
from lup.runtime.factory import SessionFactory
from lup.runtime.routing import ModelRoute, ModelRouter, PrefixModelMatcher

from examples.common import Summary

MODEL = "claude-opus-5"  # lup: ignore[constant-declaration] — a vendor's model id


def claude_factory() -> SessionFactory:
    return create_claude_session_factory(
        ClaudeSessionConfig(
            model=MODEL,
            system_prompt="Submit a concise structured summary.",
        )
    )


def codex_factory() -> SessionFactory:
    return create_codex_session_factory(
        CodexSessionConfig(
            model="gpt-5.5",
            developer_instructions="Submit a concise structured summary.",
            cwd=Path.cwd(),
        )
    )


async def main() -> None:
    router = ModelRouter(
        [
            ModelRoute(
                name="claude",
                matcher=PrefixModelMatcher("claude-"),
                recipe=claude_factory,
            ),
            ModelRoute(
                name="codex",
                matcher=PrefixModelMatcher("gpt-"),
                recipe=codex_factory,
            ),
        ]
    )
    result = await router.resolve(MODEL).query(
        "Explain explicit model routing.", Summary
    )
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
