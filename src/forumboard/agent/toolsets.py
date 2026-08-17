"""Single source of truth for the agent's MCP tool groups.

A *toolset* is the session's tools sorted into named groups, not one flat list.
The grouping is what MCP needs: each group becomes one MCP server, the group
name becomes the server name, and a tool ``foo`` in group ``worldview`` is
addressed as ``mcp__worldview__foo`` on every backend. Grouping is also what
lets the policy withhold a whole capability at once — with no Notion token
configured, the worldview group is simply not served.

This project has one tool-using agent, the worldview builder, so there is one
group. The reviewer and the editor take no tools at all: they are one-shot
structured-output calls over text that is already in hand, and a tool would
only give them a way to reach something they should not.
"""

from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from lup.mcp import LupMcpTool

ServerGroup = Literal["worldview"]
"""A tool-group name this registry can build — the vocabulary every consumer
shares: server registration, subprocess serving, and CLI selection
(``lup-devtools agent serve-tools --server``). A plain alias, not a ``type``
statement, so typer can read the choices off the annotation."""

WORLDVIEW_GROUP: ServerGroup = "worldview"


class SessionToolset(TypedDict):
    """Return type of :func:`build_session_toolset`."""

    groups: dict[ServerGroup, list["LupMcpTool"]]


def tool_group_names() -> list[ServerGroup]:
    """Group names served to subprocess backends (Codex/OpenAI)."""
    return [WORLDVIEW_GROUP]


def build_session_toolset(
    *,
    worldview_tools: list["LupMcpTool"] | None = None,
    subagent_tool: "LupMcpTool | None" = None,
) -> SessionToolset:
    """Build every MCP tool group for one session.

    ``worldview_tools`` are built against a live Notion workspace by the
    pipeline that owns one, and passed in rather than constructed here: this
    module knows the shape of a session's tools, not how to reach a workspace.
    A session given none — an inspection, or a run with no token configured —
    gets an empty group rather than a broken one.

    Args:
        worldview_tools: The Notion-backed tools, from
            :func:`forumboard.agent.tools.worldview.create_worldview_tools`.
        subagent_tool: The delegation tool, on backends without native
            subagents.

    Returns:
        The groups, keyed by server name.
    """
    tools = list(worldview_tools or [])
    if subagent_tool is not None:
        tools.append(subagent_tool)
    groups: dict[ServerGroup, list[LupMcpTool]] = {WORLDVIEW_GROUP: tools}
    return SessionToolset(groups=groups)
