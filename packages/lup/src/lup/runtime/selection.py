"""Selecting which runtime answers a session, as one value.

An application that names a runtime in more than one place can be changed in
only one of them: the sessions it opens would come from Codex while the login
its profile system administers still belonged to Claude. A :class:`Runtime`
is the whole selection — what a session opens through, and where that runtime
keeps a login — so an application holds one and names a provider nowhere.

:class:`SessionRequest` is a declaration, not a configuration. It says what a
caller wants of a session in words every runtime shares; each runtime renders
it into its own shape and states, in its own docstring, what it has no words
for. A field added here is a request every runtime then has to answer.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.hooks import LupHooksConfig
from lup.mcp import McpServerEntry
from lup.runtime.factory import SessionFactory
from lup.runtime.login import ProviderLogin
from lup.types import EnvVars

type SessionAutonomy = Literal["ask", "accept_edits", "plan", "unattended"]
"""How much a session may do before it stops to ask.

Named for what a caller wants rather than for either runtime's own control:
one spells this as a permission mode over tools, the other as a sandbox its
approvals are decided against, and a caller wanting an unattended session
should not have to know which.
"""


class SessionRequest(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What an application asks of a session, before a runtime renders it."""

    model: str | None = None
    instructions: str = ""
    """The standing instructions a session opens with, however it spells them."""

    cwd: Path | None = None
    autonomy: SessionAutonomy | None = None
    tools: list[str] | None = None
    allowed_tools: list[str] = []
    tool_servers: dict[str, McpServerEntry] = {}
    max_turns: int | None = None
    max_thinking_tokens: int | None = None
    environment: EnvVars = {}
    hooks: LupHooksConfig | None = None


type SessionOpener = Callable[[SessionRequest], SessionFactory]
"""Render one request into the configured session factory of one runtime."""


class Runtime(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One runtime an application selects, end to end.

    A transparent carrier: it decides nothing and composes no seam, so an
    application stores one the way it stores any other declaration, and the
    single assignment naming it is the only place a provider is chosen.
    """

    name: str
    login: ProviderLogin
    open: SessionOpener

    def session_factory(self, request: SessionRequest) -> SessionFactory:
        """Open a session factory for this runtime from a portable request."""
        return self.open(request)
