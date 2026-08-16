"""Which tool answers a URL better than fetching it would.

A fetch tool that always fetches is worse than the tools standing next to it:
behind a prediction-market URL is a market tool, behind a paper URL is a paper
tool, and the rendered page is a poorer answer than either API. This is the
table that says so — a URL shape, the tool it stands for, and how to build
that tool's arguments out of what the shape matched.

The table's *content* belongs to the adopting project, which is the only thing
that knows its own tools; what lives here is the matching and the dispatch. A
domain whose better tool cannot be reached from the URL alone registers a
redirection instead: no call, just the sentence naming what to reach for.

Registration sits beside each tool's own definition rather than in one central
list, so adding a route is a local edit and no import list has to stay in
sync::

    >>> routes.route(
    ...     r"arxiv\\.org/abs/(\\d+\\.\\d+)",
    ...     fetch_arxiv,
    ...     lambda m: {"paper_id": m.group(1)},
    ... )
    >>> routes.redirect("scholar.google.com", "Use search_arxiv instead.")

A fetch tool consults both before reaching the network: :meth:`ToolRoutes.dispatch`
answers with the better tool's own response, and :meth:`ToolRoutes.advice`
answers with the sentence to hand back instead.
"""

import logging

# lup: ignore[import-re] — caller-supplied URL shapes are this module's subject
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel

from lup.mcp import LupMcpTool, ToolResponse
from lup.types import JsonObject

logger = logging.getLogger(__name__)

type ParamBuilder = Callable[[re.Match[str]], JsonObject]
"""Builds a tool's arguments from what its URL pattern matched."""


class ToolRoute(BaseModel, arbitrary_types_allowed=True):
    """One URL shape and the tool call it stands for."""

    pattern: re.Pattern[str]
    tool: LupMcpTool
    build_params: ParamBuilder

    def arguments(self, url: str) -> JsonObject | None:
        """The arguments this route would call its tool with, or None.

        Separate from dispatch so a caller can ask whether a URL is covered
        without making the call — and so the match is testable on its own.
        """
        found = self.pattern.search(url)
        if found is None:
            return None
        return self.build_params(found)


class Redirection(BaseModel):
    """A domain with a better tool that no URL mapping can reach.

    Some sites have a dedicated tool whose arguments simply are not in the
    URL — a ticker page whose symbol is rendered by script, a search page
    whose query is a session. Nothing can be dispatched, but answering with
    the page would still be the wrong move, so the agent is told what to
    reach for instead.
    """

    domain: str
    advice: str

    def covers(self, url: str) -> bool:
        """Whether this redirection speaks to the URL's host.

        Matched on the parsed host rather than by substring, so a domain
        appearing anywhere else in the URL — in a path, a query parameter, or
        a lookalike registration ending in it — does not answer for it.
        """
        host = urlsplit(url).hostname or ""
        return host == self.domain or host.endswith(f".{self.domain}")


class ToolRoutes(BaseModel):
    """Every route and redirection a project has registered."""

    routes: list[ToolRoute] = []
    redirections: list[Redirection] = []

    def route(self, pattern: str, tool: LupMcpTool, build_params: ParamBuilder) -> None:
        """Register the tool a URL shape should reach, beside that tool.

        *pattern* is searched against the whole URL, so it needs to carry
        only the part that identifies the resource.
        """
        self.routes.append(
            ToolRoute(
                # lup: ignore[re-call] — a URL shape, which is what a pattern is for
                pattern=re.compile(pattern),
                tool=tool,
                build_params=build_params,
            )
        )

    def redirect(self, domain: str, advice: str) -> None:
        """Register what to reach for on a domain no URL mapping covers."""
        self.redirections.append(Redirection(domain=domain, advice=advice))

    async def dispatch(self, url: str) -> ToolResponse | None:
        """Call the tool this URL stands for, or None to fetch it after all.

        First registration wins, so a project orders its specific shapes
        before its general ones.
        """
        for entry in self.routes:
            arguments = entry.arguments(url)
            if arguments is not None:
                logger.info(
                    "Tool route: %s → %s(%s)", url[:60], entry.tool.name, arguments
                )
                return await entry.tool.handler(arguments)
        return None

    def advice(self, url: str) -> str | None:
        """What to reach for instead, when a domain has a better tool."""
        return next(
            (entry.advice for entry in self.redirections if entry.covers(url)), None
        )


routes = ToolRoutes()
"""The registry tools register against at import time."""
