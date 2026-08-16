"""Refuse a denied fetch inside a live session, not on a printout.

The fetch half of the policy pair; ``semantic_policy_shell`` is the
tool-call half. Both declare the same origin table and enforce it through
the session's own hook seam, so the URL a policy denies is a URL the
session cannot retrieve.

The session runs with permissions bypassed, which is the mode where this
hook is the only gate the call meets: what the policy denies is refused,
and nothing else is left to grant it.
"""

import asyncio

from pydantic import AnyHttpUrl

from lup.adapters.claude.hooks import CLAUDE_SEMANTICS
from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.hooks import LupHooksConfig
from lup.policy.enforcement import SemanticToolPolicy, create_policy_hooks
from lup.policy.rules import FetchPolicy, UrlScope
from lup.runtime.query import query

from examples.common import Summary

# lup: ignore[constant-declaration] — the one origin this example allows, which
# is the example's subject rather than a value to pass in
DOCS_ORIGIN = AnyHttpUrl("https://docs.example.com")
# lup: ignore[constant-declaration] — the one URL this example demonstrates a
# denial on, which is the example's subject rather than a value to pass in
DENIED_URL = "https://docs.example.com/private/token"


def policy_hooks() -> LupHooksConfig:
    """Enforce one declared fetch scope on every call the session attempts.

    Only the fetch family is declared, so a shell command here answers ask
    rather than allow — an undeclared family is a missing rule, not a
    permission.
    """
    policy = FetchPolicy(
        allowed=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/api")],
        denied=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/private")],
    )
    return create_policy_hooks(
        SemanticToolPolicy(fetch=policy),
        CLAUDE_SEMANTICS,
    )


def session_config() -> ClaudeSessionConfig:
    """Carry the enforcing hooks into the session the factory will open."""
    return ClaudeSessionConfig(
        model="claude-opus-5",
        system_prompt="Fetch what you are asked for and report what happened.",
        hooks=policy_hooks(),
    )


async def main() -> None:
    factory = create_claude_session_factory(session_config())
    result = await query(
        factory,
        f"Fetch {DENIED_URL} and summarize the page.",
        Summary,
    )
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
